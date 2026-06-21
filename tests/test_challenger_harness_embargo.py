"""Point-in-time embargo at the fit/calibration boundary in train_predictor.

The review flagged that the held-out calibration tail could temporally overlap
the model's training-label windows (a subtle forward-looking leak). train_predictor
now drops fit rows whose label window [t, t+horizon] reaches into the calibration
slice, with a safe fallback to the un-embargoed split when the embargo would leave
too little / single-class data.
"""
from __future__ import annotations

import math
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import challenger as ch
from q15_upgrade.challenger import features as featmod
from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.mathx import normal_cdf


def _dataset(n, seed=1, spacing=60.0):
    """Rows spaced `spacing` seconds apart with a structural label."""
    rng = random.Random(seed)
    ts, feats, y = [], [], []
    t0 = 1_700_000_000.0
    for i in range(n):
        spot = 65000 + rng.uniform(-500, 500)
        target = spot + rng.uniform(-120, 120)
        seconds = rng.choice([900, 600, 420])
        vol = 0.004
        minutes = seconds / 60.0
        nd = (target - spot) / (spot * vol * math.sqrt(minutes))
        true_p = normal_cdf(-nd)
        market = min(0.99, max(0.01, true_p + rng.uniform(-0.03, 0.03)))
        yes_mid = market * 100.0
        snap = {
            "spot": spot, "target": target, "seconds_remaining": seconds,
            "volatility_per_min": vol, "data_quality": 0.8,
            "yes_bid": yes_mid - 1, "yes_ask": yes_mid + 1,
            "no_bid": (100 - yes_mid) - 1, "no_ask": (100 - yes_mid) + 1,
            "depth_contracts": 50,
            "feature_values": {"momentum": 0.1, "flow": -0.05, "book": 0.0},
        }
        z = rng.gauss(0, 1)
        label = 1 if spot * math.exp(vol * math.sqrt(minutes) * z) >= target else 0
        ts.append(t0 + i * spacing)
        feats.append(featmod.extract(snap).details)
        y.append(label)
    return ts, feats, y


class EmbargoTest(unittest.TestCase):
    def test_boundary_rows_are_embargoed(self):
        ts, feats, y = _dataset(300, seed=5)
        cfg = ChallengerConfig().with_overrides(min_train_rows=50, horizon_seconds=900.0)
        predictor, info = ch.train_predictor(ts, feats, y, cfg, calib_tail_fraction=0.25)
        self.assertTrue(info["fitted"])
        # 60s spacing, 900s horizon -> the last ~15 fit rows reach into the
        # calibration slice and must be embargoed.
        self.assertGreater(info["embargoed_fit_rows"], 0)
        self.assertNotIn("embargo_skipped", info)

    def test_zero_horizon_embargoes_nothing(self):
        ts, feats, y = _dataset(300, seed=6)
        cfg = ChallengerConfig().with_overrides(min_train_rows=50, horizon_seconds=0.0)
        predictor, info = ch.train_predictor(ts, feats, y, cfg, calib_tail_fraction=0.25)
        self.assertTrue(info["fitted"])
        self.assertEqual(info["embargoed_fit_rows"], 0)

    def test_falls_back_when_embargo_starves_training(self):
        # With a min_train_rows just above what the embargo would leave, the
        # embargo must be skipped (fall back to the un-embargoed split) rather
        # than degrade or refuse the fit.
        ts, feats, y = _dataset(300, seed=7)
        # split = 225; embargo leaves ~211, so require more than that.
        cfg = ChallengerConfig().with_overrides(min_train_rows=215, horizon_seconds=900.0)
        predictor, info = ch.train_predictor(ts, feats, y, cfg, calib_tail_fraction=0.25)
        self.assertTrue(info["fitted"])
        self.assertTrue(info.get("embargo_skipped"))
        self.assertEqual(info["embargoed_fit_rows"], 0)


if __name__ == "__main__":
    unittest.main()
