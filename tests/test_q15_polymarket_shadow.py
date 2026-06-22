"""Polymarket up/down shadow — deterministic unit tests.

Covers the pure probability model, the ledger (record/resolve/scoring), the
compact panel formatter, and the runner orchestration via an INJECTED fake client
(no network, no worker thread). Default-OFF behavior is asserted too.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.polymarket.client import BookMetrics, PolymarketMarket, updown_slug
from q15_upgrade.polymarket.config import PolymarketConfig
from q15_upgrade.polymarket.ledger import PolymarketLedger, window_id
from q15_upgrade.polymarket.probability import updown_probability
from q15_upgrade.polymarket.runner import PolymarketRunner, build_polymarket_panel

# A :15 wall-clock boundary (divisible by 900) so window_id is exact.
CLOSE = 1_700_000_100.0
assert CLOSE % 900 == 0


def _book(**kw) -> BookMetrics:
    base = dict(yes_bid=60.0, yes_ask=62.0, no_bid=38.0, no_ask=40.0,
                spread=2.0, depth=500.0, last_trade=61.0, market_prob_up=0.61)
    base.update(kw)
    return BookMetrics(**base)


def _market(**kw) -> PolymarketMarket:
    base = dict(asset="BTC", slug=updown_slug("BTC", CLOSE - 900.0), condition_id="cond1",
                yes_token_id="yes1", no_token_id="no1", open_price=100.0,
                window_open=CLOSE - 900.0, window_close=CLOSE)
    base.update(kw)
    return PolymarketMarket(**base)


class FakeClient:
    """Deterministic stand-in for PolymarketClient (no network)."""
    def __init__(self, market=None, book=None, result=None):
        self._market = market
        self._book = book if book is not None else _book()
        self._result = result
        self.discover_calls = 0

    def find_updown_market(self, asset, close):
        self.discover_calls += 1
        return self._market

    def book_metrics(self, market):
        return self._book

    def resolved_result(self, market):
        return self._result


# --------------------------------------------------------------------------- #
class ProbabilityTest(unittest.TestCase):
    def test_above_open_is_up(self):
        p = updown_probability(spot=100.5, open_price=100.0, seconds_remaining=600,
                               sigma_per_sqrt_second=0.0005, drift_per_second=0.0)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.5)

    def test_below_open_is_down(self):
        p = updown_probability(spot=99.5, open_price=100.0, seconds_remaining=600,
                               sigma_per_sqrt_second=0.0005, drift_per_second=0.0)
        self.assertLess(p, 0.5)

    def test_at_open_is_half(self):
        p = updown_probability(spot=100.0, open_price=100.0, seconds_remaining=600,
                               sigma_per_sqrt_second=0.0005, drift_per_second=0.0)
        self.assertAlmostEqual(p, 0.5, places=6)

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(updown_probability(spot=None, open_price=100.0, seconds_remaining=600,
                                             sigma_per_sqrt_second=0.0005))
        self.assertIsNone(updown_probability(spot=100.0, open_price=0.0, seconds_remaining=600,
                                             sigma_per_sqrt_second=0.0005))
        self.assertIsNone(updown_probability(spot=100.0, open_price=100.0, seconds_remaining=0,
                                             sigma_per_sqrt_second=0.0005))
        self.assertIsNone(updown_probability(spot=100.0, open_price=100.0, seconds_remaining=600,
                                             sigma_per_sqrt_second=0.0))

    def test_drift_is_capped_to_fraction_of_sigma(self):
        # A huge positive drift cannot push P(up) past what the cap allows: with
        # spot==open the cap (0.35 sigma) bounds z to <= 0.35 -> p <= Phi(0.35).
        p = updown_probability(spot=100.0, open_price=100.0, seconds_remaining=600,
                               sigma_per_sqrt_second=0.0005, drift_per_second=1.0,
                               max_drift_fraction_of_sigma=0.35)
        self.assertLessEqual(p, _phi(0.35) + 1e-9)
        self.assertGreater(p, 0.5)

    def test_bounded_one_to_99(self):
        p = updown_probability(spot=1e9, open_price=1.0, seconds_remaining=600,
                               sigma_per_sqrt_second=0.0005)
        self.assertLessEqual(p, 0.99)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --------------------------------------------------------------------------- #
class LedgerTest(unittest.TestCase):
    def _ledger(self):
        tmp = tempfile.mkdtemp()
        return PolymarketLedger(os.path.join(tmp, "poly.sqlite3"))

    def _record(self, led, *, checkpoint="15M", model_prob_up=0.66, market_prob_up=0.61,
                condition_id="cond1"):
        return led.record(
            model_version="polymarket-test", asset="BTC", checkpoint=checkpoint,
            close_time=CLOSE, condition_id=condition_id, yes_token_id="yes1",
            spot=100.5, open_price=100.0, seconds_remaining=600.0,
            model_prob_up=model_prob_up, market_prob_up=market_prob_up,
            poly_yes_bid=60.0, poly_yes_ask=62.0, poly_no_bid=38.0, poly_no_ask=40.0,
            poly_spread=2.0, poly_depth=500.0, poly_last_trade=61.0, snapshot_id="15M@1")

    def test_record_is_idempotent_per_condition_checkpoint(self):
        led = self._ledger()
        self.assertIsNotNone(self._record(led))
        self.assertIsNone(self._record(led))                       # duplicate -> kept first
        self.assertIsNotNone(self._record(led, checkpoint="10M"))  # different checkpoint -> ok

    def test_no_condition_id_records_nothing(self):
        led = self._ledger()
        self.assertIsNone(self._record(led, condition_id=None))

    def test_resolve_grades_model_and_market(self):
        led = self._ledger()
        for cp in ("15M", "10M", "7M"):
            self._record(led, checkpoint=cp)
        n = led.resolve("cond1", "UP", model_version="polymarket-test")
        self.assertEqual(n, 3)                                     # all three checkpoints
        acc = led.accuracy_by_checkpoint(model_version="polymarket-test")
        self.assertEqual(acc["n"], 3)
        # model said UP (0.66) and market said UP (0.61); result UP -> both correct.
        self.assertEqual(acc["by_checkpoint"]["15M"]["model"]["correct"], 1)
        self.assertEqual(acc["by_checkpoint"]["15M"]["market"]["correct"], 1)

    def test_resolve_marks_wrong_side(self):
        led = self._ledger()
        self._record(led, model_prob_up=0.20, market_prob_up=0.61)  # model DOWN, market UP
        led.resolve("cond1", "UP", model_version="polymarket-test")
        acc = led.accuracy_by_checkpoint(model_version="polymarket-test")
        self.assertEqual(acc["by_checkpoint"]["15M"]["model"]["wrong"], 1)
        self.assertEqual(acc["by_checkpoint"]["15M"]["market"]["correct"], 1)

    def test_resolve_rejects_bad_result(self):
        led = self._ledger()
        self._record(led)
        self.assertEqual(led.resolve("cond1", "YES", model_version="polymarket-test"), 0)

    def test_unresolved_closed_only_after_close(self):
        led = self._ledger()
        self._record(led)
        self.assertEqual(led.unresolved_closed("polymarket-test", now=CLOSE - 1), [])
        rows = led.unresolved_closed("polymarket-test", now=CLOSE + 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["condition_id"], "cond1")

    def test_latest_window_exposes_our_side_ask(self):
        led = self._ledger()
        self._record(led, checkpoint="15M", model_prob_up=0.66)   # UP -> yes ask 62
        lw = led.latest_window("polymarket-test")
        self.assertEqual(lw["window_id"], window_id(CLOSE))
        slot = lw["checkpoints"]["15M"]
        self.assertEqual(slot["model_side"], "UP")
        self.assertEqual(slot["our_side_ask_cents"], 62.0)

    def test_archived_versions_excludes_current(self):
        led = self._ledger()
        self._record(led)                                         # polymarket-test
        led.record(model_version="polymarket-old", asset="BTC", checkpoint="15M",
                   close_time=CLOSE, condition_id="condX", yes_token_id="y",
                   spot=1.0, open_price=1.0, seconds_remaining=600.0,
                   model_prob_up=0.5, market_prob_up=0.5)
        self.assertEqual(led.archived_versions("polymarket-test"), ["polymarket-old"])


# --------------------------------------------------------------------------- #
class PanelTest(unittest.TestCase):
    def test_no_matching_market(self):
        msg = build_polymarket_panel(reset_at=CLOSE, latest={"checkpoints": {}}, accuracy={"n": 0})
        self.assertIn("POLYMARKET SHADOW", msg)
        self.assertIn("NO MATCHING MARKET", msg)

    def test_renders_sides_prices_and_edge(self):
        latest = {"window_id": 1, "close_time": CLOSE, "checkpoints": {
            "15M": {"asset": "BTC", "model_side": "UP", "model_prob_up": 0.66,
                    "market_side": "UP", "market_prob_up": 0.61,
                    "our_side_ask_cents": 62.0, "official_result": None}}}
        acc = {"n": 24, "by_checkpoint": {
            "15M": {"model": {"correct": 5, "wrong": 3, "accuracy": 0.625},
                    "market": {"correct": 4, "wrong": 4, "accuracy": 0.5}},
            "10M": {"model": {"correct": 0, "wrong": 0, "accuracy": None},
                    "market": {"correct": 0, "wrong": 0, "accuracy": None}},
            "7M": {"model": {"correct": 0, "wrong": 0, "accuracy": None},
                   "market": {"correct": 0, "wrong": 0, "accuracy": None}}}}
        msg = build_polymarket_panel(reset_at=CLOSE, latest=latest, accuracy=acc)
        self.assertIn("15M: UP 66% | Market 62¢", msg)
        self.assertIn("Pricing: Polymarket better by 4¢", msg)   # 66 - 62
        self.assertIn("Cross-market agreement: YES", msg)
        self.assertIn("Status: LEARNING ONLY", msg)
        self.assertIn("model 5W–3L 62% | market 4W–4L 50%", msg)
        self.assertIn("Sample: 24", msg)

    def test_disagreement_is_mixed(self):
        latest = {"window_id": 1, "close_time": CLOSE, "checkpoints": {
            "15M": {"asset": "BTC", "model_side": "UP", "model_prob_up": 0.66,
                    "market_side": "DOWN", "market_prob_up": 0.40,
                    "our_side_ask_cents": 62.0, "official_result": None}}}
        msg = build_polymarket_panel(reset_at=CLOSE, latest=latest, accuracy={"n": 1, "by_checkpoint": {}})
        self.assertIn("Cross-market agreement: MIXED", msg)


# --------------------------------------------------------------------------- #
class RunnerTest(unittest.TestCase):
    def _runner(self, client):
        tmp = tempfile.mkdtemp()
        cfg = PolymarketConfig.from_env().with_overrides(
            enabled=True, db_path=os.path.join(tmp, "poly.sqlite3"),
            model_version="polymarket-test", assets=("BTC",))
        return PolymarketRunner(config=cfg, client=client)

    def test_observe_records_when_market_found(self):
        r = self._runner(FakeClient(market=_market(), book=_book()))
        r._observe_sync(asset="BTC", checkpoint="15M", spot=100.5,
                        sigma_per_sqrt_second=0.0005, drift_per_second=0.0,
                        seconds_remaining=600.0, close_time=CLOSE,
                        snapshot_id="15M@1", now=CLOSE - 600)
        lw = r.ledger.latest_window("polymarket-test")
        self.assertEqual(lw["checkpoints"]["15M"]["model_side"], "UP")
        self.assertEqual(lw["checkpoints"]["15M"]["our_side_ask_cents"], 62.0)

    def test_observe_no_market_records_nothing_and_locks(self):
        client = FakeClient(market=None)
        r = self._runner(client)
        kw = dict(asset="BTC", checkpoint="15M", spot=100.5, sigma_per_sqrt_second=0.0005,
                  drift_per_second=0.0, seconds_remaining=600.0, close_time=CLOSE,
                  snapshot_id="15M@1", now=CLOSE - 600)
        r._observe_sync(**kw)
        r._observe_sync(**kw)                                     # second call: cached/locked
        self.assertEqual(r.ledger.latest_window("polymarket-test")["checkpoints"], {})
        self.assertEqual(client.discover_calls, 1)                # discovery cached per window

    def test_unconfigured_asset_is_skipped(self):
        r = self._runner(FakeClient(market=_market()))
        r._observe_sync(asset="DOGE", checkpoint="15M", spot=100.5,
                        sigma_per_sqrt_second=0.0005, drift_per_second=0.0,
                        seconds_remaining=600.0, close_time=CLOSE, snapshot_id="x", now=0)
        self.assertEqual(r.ledger.latest_window("polymarket-test")["checkpoints"], {})

    def test_reconcile_resolves_and_marks_report(self):
        client = FakeClient(market=_market(), book=_book(), result="UP")
        r = self._runner(client)
        r._observe_sync(asset="BTC", checkpoint="15M", spot=100.5,
                        sigma_per_sqrt_second=0.0005, drift_per_second=0.0,
                        seconds_remaining=600.0, close_time=CLOSE,
                        snapshot_id="15M@1", now=CLOSE - 600)
        resolved = r._reconcile_sync(now=CLOSE + 1)
        self.assertEqual(resolved, 1)
        acc = r.ledger.accuracy_by_checkpoint("polymarket-test")
        self.assertEqual(acc["by_checkpoint"]["15M"]["model"]["correct"], 1)
        # resolving stamps a pending report for the boundary
        self.assertIsNotNone(r.drain_report())

    def test_observe_with_no_open_price_records_market_only(self):
        # No price-to-beat -> our P(up) is None (graded later as no-call), but the
        # market side is still captured and gradable.
        client = FakeClient(market=_market(open_price=None), book=_book(), result="UP")
        r = self._runner(client)
        r._observe_sync(asset="BTC", checkpoint="15M", spot=100.5,
                        sigma_per_sqrt_second=0.0005, drift_per_second=0.0,
                        seconds_remaining=600.0, close_time=CLOSE,
                        snapshot_id="15M@1", now=CLOSE - 600)
        r._reconcile_sync(now=CLOSE + 1)
        acc = r.ledger.accuracy_by_checkpoint("polymarket-test")
        self.assertEqual(acc["by_checkpoint"]["15M"]["market"]["correct"], 1)
        self.assertEqual(acc["by_checkpoint"]["15M"]["model"]["correct"], 0)
        self.assertEqual(acc["by_checkpoint"]["15M"]["model"]["wrong"], 0)  # no model call


class ConfigTest(unittest.TestCase):
    def test_default_off(self):
        # With no Q15_POLYMARKET_* env set, the package is inert.
        for k in list(os.environ):
            if k.startswith("Q15_POLYMARKET_"):
                os.environ.pop(k, None)
        self.assertFalse(PolymarketConfig.from_env().enabled)


if __name__ == "__main__":
    unittest.main()
