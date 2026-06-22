from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger


def _record_closed(led, ticker):
    """Record a prediction whose market has already closed (eligible to settle)."""
    return led.record_prediction(
        ticker=ticker, asset="ETH", checkpoint="15M", created_at=time.time() - 900,
        close_time=time.time() - 30, predicted_side="YES",
        raw_yes_probability=0.7, calibrated_yes_probability=0.7, challenger_yes_probability=0.7,
        baseline_yes_probability=0.6, selected_probability=0.7, conservative_probability=0.65,
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3}, contributions={"momentum": 0.2}, quote={"ask_cents": 50},
        rank=1,
    )


class TestReconcileBudget(unittest.TestCase):
    def setUp(self):
        self.led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        for i in range(6):
            _record_closed(self.led, f"T-{i}")
        os.environ.pop("Q15_V95_RECONCILE_BUDGET_SECONDS", None)

    def tearDown(self):
        os.environ.pop("Q15_V95_RECONCILE_BUDGET_SECONDS", None)

    def test_slow_lookups_stop_at_budget(self):
        os.environ["Q15_V95_RECONCILE_BUDGET_SECONDS"] = "0.5"  # the minimum budget
        calls = {"n": 0}

        def slow_get_market(ticker):
            calls["n"] += 1
            time.sleep(0.6)            # one lookup already exceeds the budget
            return {"result": ""}      # not officially resolved yet

        out = self.led.reconcile_pending_from_market(slow_get_market)
        # 6 are eligible, but a single slow lookup blows the budget -> it stops.
        self.assertTrue(out["budget_exceeded"])
        self.assertLess(out["market_calls"], 6)
        self.assertLessEqual(calls["n"], 2)
        self.assertEqual(out["new_predictions_resolved"], 0)

    def test_fast_resolved_markets_all_grade(self):
        os.environ["Q15_V95_RECONCILE_BUDGET_SECONDS"] = "30"
        seen = []

        def fast_get_market(ticker):
            seen.append(ticker)
            return {"result": "YES", "close_time": time.time() - 20}

        out = self.led.reconcile_pending_from_market(fast_get_market)
        self.assertFalse(out["budget_exceeded"])
        self.assertEqual(out["market_calls"], 6)
        self.assertEqual(out["new_predictions_resolved"], 6)

    def test_default_budget_applies_without_env(self):
        out = self.led.reconcile_pending_from_market(lambda t: {"result": ""})
        self.assertEqual(out["budget_seconds"], 4.0)  # default
        self.assertIn("elapsed_seconds", out)

    def test_undetermined_closed_markets_are_captured_for_diagnosis(self):
        # A closed market whose `result` is empty but which exposes Kalshi's instant
        # determination fields must be CAPTURED (raw fields + key list), so we can see
        # which field carries the determined outcome — not silently skipped.
        os.environ["Q15_V95_RECONCILE_BUDGET_SECONDS"] = "30"

        def determined_but_unsettled(ticker):
            return {"result": "", "status": "closed", "settlement_value": 64250.0,
                    "floor_strike": 64000.0, "strike_type": "greater_or_equal",
                    "close_time": time.time() - 20}

        out = self.led.reconcile_pending_from_market(determined_but_unsettled)
        # Nothing graded (we don't act on a guessed field yet) but everything captured.
        self.assertEqual(out["new_predictions_resolved"], 0)
        self.assertEqual(out["undetermined_closed_count"], 6)
        sample = out["undetermined_market_samples"][0]
        self.assertEqual(sample["status"], "closed")
        self.assertEqual(sample["settlement_value"], 64250.0)
        self.assertEqual(sample["floor_strike"], 64000.0)
        self.assertIn("settlement_value", sample["all_keys"])  # full key list present

    def test_undetermined_sample_limit_is_bounded(self):
        os.environ["Q15_V95_RECONCILE_BUDGET_SECONDS"] = "30"
        os.environ["Q15_V95_UNDETERMINED_SAMPLE_LIMIT"] = "2"
        try:
            out = self.led.reconcile_pending_from_market(lambda t: {"result": "", "status": "closed"})
            self.assertEqual(len(out["undetermined_market_samples"]), 2)  # bounded
            self.assertEqual(out["undetermined_closed_count"], 2)
        finally:
            os.environ.pop("Q15_V95_UNDETERMINED_SAMPLE_LIMIT", None)


if __name__ == "__main__":
    unittest.main()
