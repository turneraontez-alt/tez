from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import (
    analyse_v95,
    build_canonical_snapshot,
    build_v95_message,
    rank_analyses,
    _humanize_v95_reasons,
)
from q15_upgrade.ledger_v95 import V95Ledger
from tests.test_q15_v95 import NOW, candles, context, public, snapshot


def _analyse(snap, asset, ledger, *, cached=None, ctx=None, pub=None):
    canonical = build_canonical_snapshot(
        snap, asset=asset, checkpoint="10M", now=NOW,
        cached_candles=candles() if cached is None else cached,
        context=context() if ctx is None else ctx,
        public=public() if pub is None else pub,
    )
    return analyse_v95(snap, canonical, ledger)


class TestDegradedV95Message(unittest.TestCase):
    def setUp(self):
        self.ledger = V95Ledger()

    def test_reason_humanizer(self):
        self.assertEqual(
            _humanize_v95_reasons("missing_or_invalid_threshold"),
            "strike/threshold not set yet",
        )
        # multiple comma-joined tokens, deduped, unknown tokens prettified
        self.assertEqual(
            _humanize_v95_reasons("missing_or_invalid_spot,some_new_reason"),
            "underlying spot price unavailable; some new reason",
        )
        self.assertIn("incomplete", _humanize_v95_reasons(""))

    def test_data_outage_row_is_clean(self):
        bad = snapshot(asset="ETH", checkpoint="10M")
        bad["underlying_current"] = None
        bad["target_value"] = None
        analysis = _analyse(bad, "ETH", self.ledger, cached=[], ctx={}, pub={})
        self.assertFalse(analysis.get("prediction_available"))

        analyses = {"ETH": analysis}
        msg = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())

        self.assertIn("no prediction", msg)
        self.assertIn("strike/threshold not set yet", msg)
        # The literal None / n/a wall must be gone for the unavailable row.
        self.assertNotIn("ETH None", msg)
        self.assertNotIn("None ask:", msg)
        self.assertNotIn("n/a", msg)

    def test_healthy_row_unaffected(self):
        good = snapshot(asset="BNB", checkpoint="10M", ask=52.0, target=100.0, spot=101.0)
        analysis = _analyse(good, "BNB", self.ledger)
        self.assertTrue(analysis.get("prediction_available"))
        analyses = {"BNB": analysis}
        msg = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())
        self.assertIn("BNB YES", msg)        # the pick + side (in the headline)
        self.assertIn("ENTRY", msg)          # the action
        self.assertIn("edge", msg)           # the economics
        self.assertNotIn("no prediction", msg)
        self.assertNotIn("None", msg)

    def test_message_uses_hourly_report_style_table(self):
        # Two healthy picks should render the aligned <pre> monospace table that
        # mirrors the hourly report's look, plus the required header markers.
        good_a = snapshot(asset="BNB", checkpoint="10M", ask=52.0, target=100.0, spot=101.0)
        good_b = snapshot(asset="BTC", checkpoint="10M", ask=48.0, target=100.0, spot=101.0)
        analyses = {
            "BNB": _analyse(good_a, "BNB", self.ledger),
            "BTC": _analyse(good_b, "BTC", self.ledger),
        }
        msg = build_v95_message("10M", analyses, rank_analyses(analyses), self.ledger.status())
        # Header invariants (suppression + formatter keys).
        self.assertIn("V9.5 CHECK", msg)
        self.assertTrue(("NO ENTRY YET" in msg) or ("ENTRY RECOMMENDED" in msg))
        # Hourly-report-style table.
        self.assertIn("<pre>", msg)
        self.assertIn("</pre>", msg)
        self.assertIn("Top picks", msg)
        self.assertIn("Side", msg)
        self.assertIn("Edge", msg)
        self.assertIn("Best:", msg)          # the one-line headline
        # Must NOT carry the hourly-report reroute marker.
        self.assertNotIn("Hourly Report —", msg)
        self.assertNotIn("None", msg)


if __name__ == "__main__":
    unittest.main()
