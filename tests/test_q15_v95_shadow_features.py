"""Regression: the read-only shadow challenger must receive decision-time context.

The production prediction carries spot / strike / time-remaining / volatility only
inside the analysis ``quote`` bundle. If those are dropped, the challenger's
distance-to-target and time-decay features (the dominant drivers of a 15-minute
binary) extract as zeros and it learns blind. These tests lock the context into
``analysis['quote']`` and verify it resurrects the challenger's feature vector.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import analyse_v95, build_canonical_snapshot, _shadow_vol_per_min
from q15_upgrade.ledger_v95 import V95Ledger
from q15_upgrade.challenger import features as featmod
from q15_upgrade.challenger.runner import _build_snapshot

from test_q15_v95 import NOW, candles, context, public, snapshot


class ShadowVolPerMinTest(unittest.TestCase):
    def test_converts_sigma_per_sqrt_second_to_per_minute(self):
        # per-minute sigma = sigma_per_sqrt_second * sqrt(60)
        self.assertAlmostEqual(
            _shadow_vol_per_min({"sigma_per_sqrt_second": 0.0005}),
            0.0005 * math.sqrt(60.0), places=10)

    def test_missing_or_nonpositive_returns_none(self):
        self.assertIsNone(_shadow_vol_per_min(None))
        self.assertIsNone(_shadow_vol_per_min({}))
        self.assertIsNone(_shadow_vol_per_min({"sigma_per_sqrt_second": 0.0}))
        self.assertIsNone(_shadow_vol_per_min({"sigma_per_sqrt_second": "x"}))


class ShadowQuoteContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()
        import q15_upgrade.checkpoint_v95 as _cp
        with _cp._LATEST_LOCK:
            _cp._LATEST_ANALYSES.clear()
            _cp._LATEST_RANKING.clear()
            _cp._LATEST_LEDGER.clear()
            _cp._LATEST_CHECKPOINT = "UNKNOWN"

    def _analyse(self, spot=102.0, target=100.0):
        row = snapshot(spot=spot, target=target)
        canonical = build_canonical_snapshot(
            row, asset=str(row.get("asset") or "BNB"), checkpoint="10M", now=NOW,
            cached_candles=candles(), context=context(), public=public(),
        )
        return analyse_v95(row, canonical, self.ledger)

    def test_quote_carries_decision_time_context(self):
        result = self._analyse()
        quote = result["quote"]
        self.assertGreater(quote["spot"], 0)
        self.assertGreater(quote["target"], 0)
        self.assertIsNotNone(quote["seconds_remaining"])
        # bid/ask still present for existing readers
        self.assertIn("ask_cents", quote)

    def test_shadow_features_are_populated_not_zero(self):
        result = self._analyse(spot=102.0, target=100.0)
        # Reconstruct exactly what the shadow runner sees at observe time.
        snap = _build_snapshot(result["feature_values"], result["quote"])
        fv = featmod.extract(snap)
        d = fv.details
        names = featmod.FEATURE_NAMES
        cov = dict(zip(names, fv.coverage))

        # The distance / time family must now be live (the whole point of the fix).
        self.assertNotEqual(d["pct_distance"], 0.0)
        self.assertNotEqual(d["log_distance"], 0.0)
        self.assertEqual(d["direction"], -1.0)  # target below spot
        self.assertGreater(d["minutes_remaining"], 0.0)
        self.assertGreater(d["sqrt_minutes_remaining"], 0.0)
        self.assertTrue(cov["pct_distance"])
        self.assertTrue(cov["minutes_remaining"])
        # normalized_distance needs a real volatility to be trustworthy.
        self.assertNotEqual(d["normalized_distance"], 0.0)
        self.assertTrue(cov["volatility_per_min"])

    def test_without_context_features_collapse(self):
        # Guard: prove the assertion above is meaningful — a bid/ask-only quote
        # (the old behaviour) leaves the distance/time family at zero.
        result = self._analyse()
        bare_quote = {k: result["quote"][k] for k in ("bid_cents", "ask_cents", "spread_cents")
                      if k in result["quote"]}
        snap = _build_snapshot(result["feature_values"], bare_quote)
        fv = featmod.extract(snap)
        self.assertEqual(fv.details["pct_distance"], 0.0)
        self.assertEqual(fv.details["minutes_remaining"], 0.0)


if __name__ == "__main__":
    unittest.main()
