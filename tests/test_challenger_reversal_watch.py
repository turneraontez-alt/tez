from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.decision import CostBreakdown, Decision
from q15_upgrade.challenger.predictor import ChallengerPrediction
from q15_upgrade.challenger.reversal_watch import REVERSAL_WATCH_MARKER, ReversalWatch
from q15_upgrade.challenger.runner import ShadowRunner


def _pred(action="BUY_YES", side="YES", ask=40.0, prob=0.65, net=20.0):
    dec = Decision(
        action=action, side=side, market_yes_prob=0.40,
        model_side_prob=prob if action != "NO_TRADE" else None,
        executable_ask_cents=ask, gross_edge_cents=25.0, net_edge_cents=net,
        costs=CostBreakdown(1.1, 0.0, 0.5, 0.25, 1.0),
        hypothetical_size_fraction=0.02 if action != "NO_TRADE" else 0.0,
    )
    return ChallengerPrediction(
        prob_yes=prob, prob_no=1.0 - prob, confidence=0.8,
        edge_vs_market=0.25, net_edge_cents=net, recommendation=action,
        top_factors=[], warnings=[], market_yes_prob=0.40, decision=dec,
    )


def _watch(tmp, **kw):
    # The pocket-record line reads the shadow ledger's table; create that schema
    # the same way production does (the ShadowLedger owns it in the same file).
    from q15_upgrade.challenger.ledger import ShadowLedger
    db = os.path.join(tmp, "shadow.sqlite3")
    ShadowLedger(db)
    args = dict(model_version="challenger-test")
    args.update(kw)
    return ReversalWatch(db, **args)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.w = _watch(self.tmp)

    def test_fires_on_exact_preregistered_rule(self):
        msg = self.w.consider(_pred(), ticker="KXBTC15M-T1", asset="BTC",
                              checkpoint="10M", close_time=1_700_000_900,
                              created_at=1_700_000_000)
        self.assertIsNotNone(msg)
        self.assertIn(REVERSAL_WATCH_MARKER, msg)
        self.assertIn("BTC 10M", msg)
        self.assertIn("BUY YES @ 40c", msg)
        self.assertIn("no order placed", msg)

    def test_rejects_15m_checkpoint(self):
        self.assertIsNone(self.w.consider(
            _pred(), ticker="T2", asset="BTC", checkpoint="15M",
            close_time=None, created_at=1.0))

    def test_rejects_non_major_assets(self):
        for asset in ("HYPE", "SOL", "BNB", "XRP"):
            self.assertIsNone(self.w.consider(
                _pred(), ticker=f"T3-{asset}", asset=asset, checkpoint="7M",
                close_time=None, created_at=1.0))

    def test_rejects_ask_at_or_above_ceiling(self):
        self.assertIsNone(self.w.consider(
            _pred(ask=45.0), ticker="T4a", asset="BTC", checkpoint="7M",
            close_time=None, created_at=1.0))
        self.assertIsNone(self.w.consider(
            _pred(ask=52.0), ticker="T4b", asset="ETH", checkpoint="10M",
            close_time=None, created_at=1.0))
        # just under the ceiling still fires
        self.assertIsNotNone(self.w.consider(
            _pred(ask=44.9), ticker="T4c", asset="ETH", checkpoint="10M",
            close_time=None, created_at=1.0))

    def test_rejects_no_trade_and_buy_no(self):
        self.assertIsNone(self.w.consider(
            _pred(action="NO_TRADE", side=None), ticker="T5a", asset="BTC",
            checkpoint="7M", close_time=None, created_at=1.0))
        self.assertIsNone(self.w.consider(
            _pred(action="BUY_NO", side="NO"), ticker="T5b", asset="DOGE",
            checkpoint="7M", close_time=None, created_at=1.0))

    def test_idempotent_per_contract_checkpoint(self):
        first = self.w.consider(_pred(), ticker="T6", asset="BTC",
                                checkpoint="7M", close_time=None, created_at=1.0)
        self.assertIsNotNone(first)
        # same contract+checkpoint never re-fires (restart-safe via the claim table)
        again = self.w.consider(_pred(), ticker="T6", asset="BTC",
                                checkpoint="7M", close_time=None, created_at=2.0)
        self.assertIsNone(again)
        # a NEW watch over the same DB (simulating a process restart) agrees
        w2 = _watch(self.tmp)
        self.assertIsNone(w2.consider(_pred(), ticker="T6", asset="BTC",
                                      checkpoint="7M", close_time=None, created_at=3.0))
        # a different checkpoint of the same contract is a separate decision time
        self.assertIsNotNone(w2.consider(_pred(), ticker="T6", asset="BTC",
                                         checkpoint="10M", close_time=None, created_at=4.0))

    def test_pocket_record_line_in_message(self):
        msg = self.w.consider(_pred(), ticker="T7", asset="ETH",
                              checkpoint="10M", close_time=None, created_at=1.0)
        self.assertIn("Pocket record (challenger-test): no settled picks yet", msg)

    def test_pocket_record_grades_exact_slice(self):
        # one winning in-pocket row + one out-of-pocket row (ask too high)
        from q15_upgrade.challenger.ledger import ShadowLedger
        db = os.path.join(self.tmp, "shadow.sqlite3")
        led = ShadowLedger(db)
        for ticker, ask, result in (("IN", 40.0, "YES"), ("OUT", 60.0, "NO")):
            led.record(_pred(ask=ask), asset="BTC", contract=ticker, checkpoint="7M",
                       control_prob_yes=0.5, created_at=1.0, close_time=1_700_000_900,
                       model_version="challenger-test", lineage={}, snapshot_id=None)
            led.resolve(ticker, "7M", result, settled_at=1_700_000_901,
                        model_version="challenger-test")
        rec = self.w.pocket_record()
        self.assertEqual(rec["n"], 1)          # only the in-pocket row counts
        self.assertEqual(rec["win_rate"], 1.0)
        self.assertIsNotNone(rec["avg_pnl_cents"])


class _StubPredictor:
    class model:
        fitted = True

    class calibrator:
        name = "identity"

    def predict(self, snap):
        return _pred()


class RunnerIntegrationTest(unittest.TestCase):
    def _runner(self, tmp, **overrides):
        cfg = ChallengerConfig().with_overrides(
            enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
            refit_every=0, model_version="challenger-test", **overrides)
        return ShadowRunner(cfg)

    def test_observe_queues_and_drains_once(self):
        tmp = tempfile.mkdtemp()
        r = self._runner(tmp, reversal_watch_enabled=True)
        self.assertIsNotNone(r._reversal)
        r.predictor = _StubPredictor()
        r.observe(ticker="KXBTC15M-R1", asset="BTC", checkpoint="7M",
                  created_at=1_700_000_000, close_time=1_700_000_900,
                  control_prob_yes=0.5,
                  features={"momentum": 0.1},
                  quote={"yes_bid_cents": 38, "yes_ask_cents": 40,
                         "no_bid_cents": 58, "no_ask_cents": 60})
        alerts = r.drain_reversal_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn(REVERSAL_WATCH_MARKER, alerts[0])
        self.assertEqual(r.drain_reversal_alerts(), [])  # drained once

    def test_disabled_by_default(self):
        tmp = tempfile.mkdtemp()
        r = self._runner(tmp)
        self.assertIsNone(r._reversal)
        self.assertEqual(r.drain_reversal_alerts(), [])

    def test_gate_failure_queues_nothing(self):
        tmp = tempfile.mkdtemp()
        r = self._runner(tmp, reversal_watch_enabled=True)
        r.predictor = _StubPredictor()
        # HYPE is outside the preregistered majors -> no alert
        r.observe(ticker="KXHYPE15M-R2", asset="HYPE", checkpoint="7M",
                  created_at=1_700_000_000, close_time=1_700_000_900,
                  control_prob_yes=0.5, features={"momentum": 0.1},
                  quote={"yes_bid_cents": 38, "yes_ask_cents": 40,
                         "no_bid_cents": 58, "no_ask_cents": 60})
        self.assertEqual(r.drain_reversal_alerts(), [])


if __name__ == "__main__":
    unittest.main()
