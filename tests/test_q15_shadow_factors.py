"""Cross-asset shadow factors: market aggregation + per-asset signed factors.

Pure/deterministic. These factors are recorded for the read-only factor lab only;
they never touch feature_json or a live decision.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import shadow_factors


def _a(momentum=None, flow=None):
    fv = {}
    if momentum is not None:
        fv["momentum"] = momentum
    if flow is not None:
        fv["flow"] = flow
    return {"feature_values": fv}


class ComputeMarketTest(unittest.TestCase):
    def test_means_and_leader_and_skip_missing(self):
        analyses = {
            "BTC": _a(momentum=0.4, flow=0.2),
            "ETH": _a(momentum=0.2, flow=None),     # flow missing -> skipped from flow mean
            "SOL": _a(momentum=None, flow=0.6),     # momentum missing -> skipped from mom mean
        }
        m = shadow_factors.compute_market(analyses)
        self.assertAlmostEqual(m["market_momentum"], 0.3)   # (0.4+0.2)/2
        self.assertAlmostEqual(m["market_flow"], 0.4)        # (0.2+0.6)/2
        self.assertAlmostEqual(m["leader_momentum"], 0.4)    # BTC
        self.assertEqual(m["n"], 2)

    def test_empty_yields_none(self):
        m = shadow_factors.compute_market({})
        self.assertIsNone(m["market_momentum"])
        self.assertEqual(m["n"], 0)


class ForAssetTest(unittest.TestCase):
    def test_signed_relative_factors(self):
        analyses = {"BTC": _a(momentum=0.4, flow=0.2), "ETH": _a(momentum=0.1, flow=0.0)}
        market = shadow_factors.compute_market(analyses)  # mom mean 0.25, leader 0.4
        eth = shadow_factors.for_asset("ETH", analyses["ETH"], market)
        self.assertAlmostEqual(eth["x_market_momentum"], 0.25)
        self.assertAlmostEqual(eth["x_leader_momentum"], 0.4)
        self.assertAlmostEqual(eth["x_rel_strength"], 0.1 - 0.25)   # underperforming -> negative (NO lean)
        self.assertAlmostEqual(eth["x_div_from_leader"], 0.1 - 0.4)

    def test_leader_diverges_from_itself_is_zero(self):
        analyses = {"BTC": _a(momentum=0.4, flow=0.2)}
        market = shadow_factors.compute_market(analyses)
        btc = shadow_factors.for_asset("BTC", analyses["BTC"], market)
        self.assertAlmostEqual(btc["x_div_from_leader"], 0.0)

    def test_no_market_yields_empty(self):
        self.assertEqual(shadow_factors.for_asset("ETH", _a(momentum=0.1), None), {})


if __name__ == "__main__":
    unittest.main()
