"""resolved_factor_rows export + the scripts/stats.py CLI (read-only).

Builds a real temp ledger, records + resolves a few predictions, and checks both
the factor-row export shape and that the stats command runs end-to-end against it.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger
from scripts import stats


def _resolve(led, ticker, checkpoint, side, official, *, features, asset="ETH",
             shadow_factors=None):
    raw = 0.62 if side == "YES" else 0.38
    led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=checkpoint, created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=raw, calibrated_yes_probability=raw,
        challenger_yes_probability=raw, baseline_yes_probability=0.5,
        selected_probability=raw, conservative_probability=max(0.01, raw - 0.05),
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features=features, contributions=features, quote={"ask_cents": 50}, rank=1,
        shadow_factors=shadow_factors,
    )
    led.resolve_ticker(ticker, official)


class ResolvedFactorRowsTest(unittest.TestCase):
    def test_export_shape_and_filter(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        _resolve(led, "C1", "10M", "YES", "YES", features={"wick": 0.4, "momentum": 0.2})
        _resolve(led, "C2", "7M", "NO", "NO", features={"wick": -0.5})
        allrows = led.resolved_factor_rows()
        self.assertEqual(len(allrows), 2)
        r = next(x for x in allrows if x["checkpoint"] == "10M")
        self.assertEqual(r["result"], "YES")
        self.assertEqual(r["predicted_side"], "YES")
        self.assertEqual(r["features"]["wick"], 0.4)
        self.assertIn("correct", r)
        self.assertIn("changed_before_close", r)
        # checkpoint filter
        self.assertEqual([x["checkpoint"] for x in led.resolved_factor_rows("7M")], ["7M"])

    def test_shadow_factors_merge_into_export_isolated_from_feature_json(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        _resolve(led, "C1", "10M", "YES", "YES", features={"wick": 0.4},
                 shadow_factors={"x_market_momentum": 0.3, "x_rel_strength": -0.1})
        row = led.resolved_factor_rows()[0]
        # the lab sees both the champion feature and the isolated shadow factors
        self.assertEqual(row["features"]["wick"], 0.4)
        self.assertEqual(row["features"]["x_market_momentum"], 0.3)
        self.assertEqual(row["features"]["x_rel_strength"], -0.1)

    def test_export_empty_when_unavailable(self):
        led = V95Ledger.__new__(V95Ledger)
        led._available = False
        self.assertEqual(led.resolved_factor_rows(), [])


class StatsCliTest(unittest.TestCase):
    def test_runs_end_to_end_against_a_real_ledger(self):
        path = tempfile.mktemp(suffix=".sqlite3")
        led = V95Ledger(path)
        for i in range(6):
            side = "YES" if i % 2 == 0 else "NO"
            _resolve(led, f"C{i}", "10M", side, side,
                     features={"wick": 0.4 if side == "YES" else -0.4})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = stats.main(["--ledger", path, "--min-samples", "2"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("LEDGER STATUS", out)
        self.assertIn("OFFICIAL RECORD", out)
        self.assertIn("FACTOR LAB", out)
        self.assertIn("wick", out)


if __name__ == "__main__":
    unittest.main()
