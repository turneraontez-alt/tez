from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger, _wilson_interval


def _rec(led, ticker, side, *, rank=1, cp="10M", ask=50.0, cost=2.0,
         champ=0.6, chall=0.6, base=0.5, close_in=600.0):
    return led.record_prediction(
        ticker=ticker, asset="ETH", checkpoint=cp, created_at=time.time(),
        close_time=time.time() + close_in, predicted_side=side,
        raw_yes_probability=champ, calibrated_yes_probability=champ,
        challenger_yes_probability=chall, baseline_yes_probability=base,
        selected_probability=0.7, conservative_probability=0.65,
        data_quality=0.9, evidence_quality=0.8, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3}, contributions={"momentum": 0.2},
        quote={"ask_cents": ask}, costs={"total_cents": cost}, rank=rank,
    )


class TestRealizedPnL(unittest.TestCase):
    def test_win_and_loss_pnl_after_fees(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        _rec(led, "W", "YES", ask=52.0, cost=2.0)   # win -> 100-52-2 = +46
        _rec(led, "L", "YES", ask=60.0, cost=2.0)    # loss -> -(60+2) = -62
        led.resolve_ticker("W", "YES")
        led.resolve_ticker("L", "NO")
        sb = led.scoreboard()
        self.assertEqual(sb["overall"]["pnl_n"], 2)
        self.assertEqual(sb["overall"]["realized_total_cents"], -16.0)
        self.assertEqual(sb["overall"]["realized_avg_cents"], -8.0)

    def test_missing_ask_has_no_pnl_but_still_counts_accuracy(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        led.record_prediction(
            ticker="NP", asset="ETH", checkpoint="10M", created_at=time.time(),
            close_time=time.time() + 600, predicted_side="YES",
            raw_yes_probability=0.7, calibrated_yes_probability=0.7, challenger_yes_probability=0.7,
            baseline_yes_probability=0.6, selected_probability=0.7, conservative_probability=0.65,
            data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
            trade_decision="PREDICTION_ONLY", regime="NORMAL",
            features={}, contributions={}, quote={}, rank=1,  # no ask_cents
        )
        led.resolve_ticker("NP", "YES")
        sb = led.scoreboard()
        self.assertEqual(sb["overall"]["n"], 1)            # graded for accuracy
        self.assertEqual(sb["overall"]["pnl_n"], 0)        # but no P&L


class TestWilsonInterval(unittest.TestCase):
    def test_small_sample_is_wide_and_flagged(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        _rec(led, "A", "YES"); led.resolve_ticker("A", "YES")
        bucket = led.scoreboard()["by_asset"]["ETH"]
        self.assertTrue(bucket["low_n"])
        self.assertLess(bucket["ci_low"], 0.5)   # 1/1=100% but interval is wide
        self.assertLessEqual(bucket["ci_high"], 1.0)

    def test_interval_math(self):
        low, high = _wilson_interval(50, 100)
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)
        self.assertEqual(_wilson_interval(0, 0), (None, None))


class TestDirectMarketSettlement(unittest.TestCase):
    def test_resolves_closed_markets_without_signals_table(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        _rec(led, "PAST-YES", "YES", close_in=-10)   # already closed
        _rec(led, "PAST-NO", "NO", close_in=-10)
        _rec(led, "FUTURE", "YES", close_in=600)      # not closed yet

        results = {
            "PAST-YES": {"result": "YES", "status": "settled"},
            "PAST-NO": {"result": "NO", "status": "settled"},
            "FUTURE": {"result": "", "status": "active"},
        }
        out = led.reconcile_pending_from_market(lambda t: results.get(t), now=time.time())
        self.assertTrue(out["available"])
        self.assertEqual(out["new_predictions_resolved"], 2)   # only the two past ones
        self.assertEqual(out["tickers_checked"], 2)            # future not even queried
        sb = led.scoreboard()
        self.assertEqual(sb["overall"]["n"], 2)
        self.assertEqual(sb["by_asset"]["ETH"]["right"], 2)    # both predicted correctly

    def test_unavailable_get_market_is_safe(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        self.assertFalse(led.reconcile_pending_from_market(None)["available"])


class TestStatisticalPromotion(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V95_PROMOTION_MIN_ROWS"] = "20"
        os.environ["Q15_V95_10M_SHADOW_LEARNING"] = "true"

    def tearDown(self):
        os.environ.pop("Q15_V95_PROMOTION_MIN_ROWS", None)
        os.environ.pop("Q15_V95_10M_SHADOW_LEARNING", None)

    def _fill(self, led, *, chall_better):
        for i in range(22):
            res = "YES" if i % 2 == 0 else "NO"
            yes = 1.0 if res == "YES" else 0.0
            # champion weak (0.55 toward truth); challenger strong when chall_better
            champ = 0.55 if yes else 0.45
            chall = (0.9 if yes else 0.1) if chall_better else champ
            _rec(led, f"T{i}", res, champ=champ, chall=chall, base=0.5)
            led.resolve_ticker(f"T{i}", res)

    def test_significant_improvement_is_promotion_candidate(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        self._fill(led, chall_better=True)
        promo = led.metrics()["promotion_by_checkpoint"]["10M"]
        self.assertTrue(promo["candidate"])
        self.assertEqual(promo["reason"], "eligible_for_manual_review")
        self.assertTrue(promo["vs_champion"]["favored"])
        self.assertLess(promo["vs_champion"]["p_value"], 0.05)

    def test_no_improvement_is_not_promoted(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        self._fill(led, chall_better=False)
        promo = led.metrics()["promotion_by_checkpoint"]["10M"]
        self.assertFalse(promo["candidate"])
        self.assertEqual(promo["reason"], "challenger_not_significantly_better")


if __name__ == "__main__":
    unittest.main()


class TestRegimeAwareChallenger(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V95_REGIME_MIN_UPDATES"] = "10"
        os.environ["Q15_V95_10M_SHADOW_LEARNING"] = "true"

    def tearDown(self):
        os.environ.pop("Q15_V95_REGIME_MIN_UPDATES", None)
        os.environ.pop("Q15_V95_10M_SHADOW_LEARNING", None)

    def _rec(self, led, ticker, regime, momentum, result):
        led.record_prediction(
            ticker=ticker, asset="ETH", checkpoint="10M", created_at=time.time(),
            close_time=time.time() + 600, predicted_side="YES",
            raw_yes_probability=0.5, calibrated_yes_probability=0.5, challenger_yes_probability=0.5,
            baseline_yes_probability=0.5, selected_probability=0.5, conservative_probability=0.5,
            data_quality=0.9, evidence_quality=0.8, trade_quality=0.7,
            trade_decision="ENTRY_RECOMMENDED", regime=regime,
            features={"momentum": momentum}, contributions={"momentum": 0.1},
            quote={"ask_cents": 50}, rank=1,
        )
        led.resolve_ticker(ticker, result)

    def test_regimes_specialize_while_global_averages_out(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        # In TREND momentum predicts YES; in NORMAL the same momentum predicts NO.
        for i in range(12):
            self._rec(led, f"TR{i}", "TREND", 0.5, "YES")
            self._rec(led, f"NM{i}", "NORMAL", 0.5, "NO")

        glob = led.challenger_weights("10M")["momentum"]
        trend = led.challenger_weights("10M", "TREND")["momentum"]
        normal = led.challenger_weights("10M", "NORMAL")["momentum"]

        # Global barely moves (signals cancel); regimes diverge in opposite directions.
        self.assertAlmostEqual(glob, 0.34, delta=0.03)
        self.assertGreater(trend, glob + 0.05)
        self.assertLess(normal, glob - 0.05)
        self.assertGreater(trend, normal)

    def test_immature_regime_falls_back_to_global(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        for i in range(12):
            self._rec(led, f"TR{i}", "TREND", 0.5, "YES")
        # A regime with no data returns exactly the global challenger weights.
        self.assertEqual(led.challenger_weights("10M", "HIGH_VOLATILITY"), led.challenger_weights("10M"))
        # Before maturity (< 10 results) a regime also falls back.
        self._rec(led, "VOL0", "HIGH_VOLATILITY", 0.5, "YES")
        self.assertEqual(led.challenger_weights("10M", "HIGH_VOLATILITY"), led.challenger_weights("10M"))

    def test_status_reports_active_regimes(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        for i in range(11):
            self._rec(led, f"TR{i}", "TREND", 0.3, "YES")
        regimes = {r["regime"]: r for r in led.status()["regime_challengers"]}
        self.assertIn("TREND", regimes)
        self.assertEqual(regimes["TREND"]["results"], 11)
        self.assertTrue(regimes["TREND"]["active"])

    def test_disabled_flag_keeps_single_global_challenger(self):
        os.environ["Q15_V95_REGIME_CHALLENGER"] = "false"
        try:
            led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
            for i in range(12):
                self._rec(led, f"TR{i}", "TREND", 0.5, "YES")
            # No regime weights written; regime query returns the global set.
            self.assertEqual(led.status()["regime_challengers"], [])
            self.assertEqual(led.challenger_weights("10M", "TREND"), led.challenger_weights("10M"))
        finally:
            os.environ.pop("Q15_V95_REGIME_CHALLENGER", None)
