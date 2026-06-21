"""Dropped-feature-row accounting in the pattern-centroid build.

The centroid build runs with the ledger lock released (heavy compute off the
hot path). The review flagged that `_dropped_feature_rows += 1` happened in that
unlocked region. The counter is now accumulated locally during the build and
folded into the shared counter under the lock. This test locks the observable
behaviour: every unparseable feature_json row is counted exactly once.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger


def _record_resolve(led, ticker, side, raw, official):
    led.record_prediction(
        ticker=ticker, asset="ETH", checkpoint="10M", created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=raw, calibrated_yes_probability=raw,
        challenger_yes_probability=raw, baseline_yes_probability=0.5,
        selected_probability=raw, conservative_probability=max(0.01, raw - 0.05),
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3, "flow": -0.1}, contributions={"momentum": 0.2},
        quote={"ask_cents": 50}, rank=1,
    )
    led.resolve_ticker(ticker, official)


class DroppedFeatureRowsTest(unittest.TestCase):
    def test_unparseable_feature_json_is_counted_once_each(self):
        path = os.path.join(tempfile.mkdtemp(), "led.sqlite3")
        led = V95Ledger(path)
        led._cache_enabled = False  # force a fresh centroid build each call
        # Seed enough resolved rows with both winners and losers to enter the
        # centroid branch (>=10 rows, >=3 winners, >=3 losers).
        for i in range(40):
            side = "YES" if i % 2 == 0 else "NO"
            official = "YES" if i % 3 == 0 else "NO"
            _record_resolve(led, f"C-{i}", side, 0.40 + (i % 7) * 0.05, official)

        feats = {"momentum": 0.3, "flow": -0.1}
        # Clean baseline: no drops.
        led.pattern_similarity(feats, "YES", "10M")
        baseline = led._dropped_feature_rows
        self.assertEqual(baseline, 0)

        # Corrupt the feature_json of 4 resolved rows directly in SQLite.
        con = sqlite3.connect(str(led.path))
        con.execute(
            "UPDATE predictions SET feature_json='{not valid json' "
            "WHERE prediction_id IN (SELECT prediction_id FROM predictions "
            "WHERE checkpoint='10M' AND official_result IS NOT NULL LIMIT 4)"
        )
        con.commit()
        con.close()

        led.pattern_similarity(feats, "YES", "10M")
        self.assertEqual(led._dropped_feature_rows - baseline, 4,
                         "each unparseable feature_json row must be counted exactly once")
        # Reported through status() too.
        self.assertEqual(led.status()["dropped_feature_rows"], led._dropped_feature_rows)


if __name__ == "__main__":
    unittest.main()
