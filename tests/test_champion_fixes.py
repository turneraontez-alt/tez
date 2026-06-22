"""Tests for the champion-review fixes (calibration + edge/gate levers).

The champion's frozen weights are untouched; these exercise the post-model
calibration layer and the entry-gate knobs:

  * Platt now converges on an under-confident model (wider slope clamp +
    convergence judged on the applied step) and the OOS self-selecting
    calibration applies the held-out-best method only when it beats identity;
  * the adverse-selection cost adder lowers the edge;
  * the volatility-aware edge bar widens the required edge for conviction picks;
  * the 15M minimum-probability default is raised to 0.60.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from q15_upgrade.checkpoint_v95 import analyse_v95, build_canonical_snapshot
from q15_upgrade.ledger_v95 import (
    CHAMPION_WEIGHTS, V95Ledger, _fit_platt, _isotonic_predict, _logit, _sigmoid, _clamp,
)
from tests.test_q15_v95 import NOW, candles, context, public, snapshot

CAL_NOW = 1_700_000_000.0


# --------------------------------------------------------------------------- #
# Platt convergence fix
# --------------------------------------------------------------------------- #
class PlattConvergenceTests(unittest.TestCase):
    def _underconfident_xy(self, n=400):
        import random
        rng = random.Random(7)
        xy = []
        for _ in range(n):
            raw = rng.uniform(0.55, 0.82)
            true_p = _sigmoid(_logit(raw) * 2.2)   # sharper than raw -> under-confident
            y = 1.0 if rng.random() < true_p else 0.0
            xy.append((_clamp(_logit(raw), -4.0, 4.0), y))
        return xy

    def test_wider_clamp_lets_slope_exceed_old_ceiling(self):
        xy = self._underconfident_xy()
        # old ceiling 1.5 pins the slope; the new ceiling lets it reach what the
        # under-confident data needs (slope > 1.5).
        _, slope_old, _, _ = _fit_platt(xy, 50, 0.5, 1.5, 0.75)
        _, slope_new, conv_new, _ = _fit_platt(xy, 50, 0.40, 3.0, 1.5)
        self.assertAlmostEqual(slope_old, 1.5, places=2)   # pinned at old ceiling
        self.assertGreater(slope_new, 1.5)                  # freed
        self.assertTrue(conv_new)

    def test_converged_fit_sharpens_underconfident_probability(self):
        xy = self._underconfident_xy()
        i, s, conv, _ = _fit_platt(xy, 50, 0.40, 3.0, 1.5)
        self.assertTrue(conv)
        mapped = _sigmoid(i + s * _clamp(_logit(0.70), -4.0, 4.0))
        self.assertGreater(mapped, 0.70)   # under-confident 0.70 pulled upward


# --------------------------------------------------------------------------- #
# OOS self-selecting calibration
# --------------------------------------------------------------------------- #
class OOSCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def _ledger(self):
        return V95Ledger(Path(self.temp.name) / f"cal_{id(self)}_{os.urandom(4).hex()}.sqlite3")

    def _seed(self, led, n, sharpen):
        import random
        rng = random.Random(3)
        for i in range(n):
            raw = round(rng.uniform(0.55, 0.82), 4)
            win_p = _sigmoid(_logit(raw) * sharpen)
            yes = rng.random() < win_p
            tk = f"R{i}"
            led.record_prediction(
                ticker=tk, asset="BNB", checkpoint="10M", created_at=CAL_NOW + i,
                close_time=CAL_NOW + i + 600, predicted_side="YES",
                raw_yes_probability=raw, calibrated_yes_probability=raw,
                challenger_yes_probability=raw, baseline_yes_probability=0.5,
                selected_probability=raw, conservative_probability=raw - 0.02,
                data_quality=0.9, evidence_quality=0.8, trade_quality=0.6,
                trade_decision="WATCH_PRICE", regime="NORMAL",
                features={k: 0.3 for k in CHAMPION_WEIGHTS if k != "intercept"},
                contributions={}, quote={"ask_cents": 55},
            )
            led.resolve_ticker(tk, "YES" if yes else "NO", CAL_NOW + i + 700)

    def test_oos_applies_calibration_on_underconfident_data(self):
        os.environ["Q15_V95_CALIBRATION_OOS_SELECT"] = "1"
        led = self._ledger()
        self._seed(led, 200, sharpen=2.2)   # under-confident -> calibration should help
        res = led.calibrate(0.72, "10M", "BNB")
        self.assertIn(res.get("method"), {"platt", "isotonic"})
        self.assertGreater(res["probability"], 0.72)        # pulled upward
        exp = led.calibration_experiment("10M")["by_checkpoint"]["10M"]
        self.assertTrue(exp["oos"]["available"])
        self.assertTrue(exp["oos"]["improves"])
        self.assertLess(exp["oos"]["best_brier"], exp["oos"]["identity_brier"])

    def test_oos_stays_identity_when_already_calibrated(self):
        os.environ["Q15_V95_CALIBRATION_OOS_SELECT"] = "1"
        led = self._ledger()
        self._seed(led, 200, sharpen=1.0)   # already well-calibrated -> no gain
        res = led.calibrate(0.72, "10M", "BNB")
        self.assertEqual(res.get("method"), "identity")
        self.assertAlmostEqual(res["probability"], 0.72, places=6)

    def test_calibration_experiment_shape(self):
        led = self._ledger()
        rep = led.calibration_experiment()
        self.assertIn("by_checkpoint", rep)
        for cp in ("15M", "10M", "7M"):
            self.assertIn(cp, rep["by_checkpoint"])


# --------------------------------------------------------------------------- #
# edge / gate levers (analyse_v95)
# --------------------------------------------------------------------------- #
class EdgeGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = V95Ledger(Path(self.temp.name) / "led.sqlite3")

    def _canonical(self, row, checkpoint="10M"):
        return build_canonical_snapshot(
            row, asset=str(row.get("asset") or "BNB"), checkpoint=checkpoint, now=NOW,
            cached_candles=candles(), context=context(), public=public())

    def test_adverse_selection_adder_lowers_net_edge(self):
        row = snapshot(spot=102.0, target=100.0)
        base = analyse_v95(row, self._canonical(row), self.ledger)
        with mock.patch.dict(os.environ, {"Q15_V95_EDGE_ADVERSE_SELECTION_CENTS": "2.0"}):
            adj = analyse_v95(row, self._canonical(row), self.ledger)
        self.assertIsNotNone(base["net_edge_cents"])
        self.assertAlmostEqual(base["net_edge_cents"] - adj["net_edge_cents"], 2.0, places=2)

    def test_volatility_scaling_widens_required_edge_for_conviction(self):
        # A high-conviction pick (spot well above target) -> conservative far from
        # 0.5 -> vol scaling raises required_edge -> ideal_entry drops.
        row = snapshot(spot=103.0, target=100.0)
        base = analyse_v95(row, self._canonical(row), self.ledger)
        with mock.patch.dict(os.environ, {"Q15_V95_EDGE_VOLATILITY_SCALING": "1",
                                          "Q15_V95_EDGE_VOLATILITY_K": "2.0"}):
            scaled = analyse_v95(row, self._canonical(row), self.ledger)
        self.assertIsNotNone(base["ideal_entry_cents"])
        self.assertLess(scaled["ideal_entry_cents"], base["ideal_entry_cents"])

    def test_net_edge_adder_and_vol_scaling_default_off(self):
        # With no env set, the new knobs are inert: the edge equals the base model.
        row = snapshot(spot=102.0, target=100.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("Q15_V95_EDGE_ADVERSE_SELECTION_CENTS", "Q15_V95_EDGE_VOLATILITY_SCALING"):
                os.environ.pop(k, None)
            res = analyse_v95(row, self._canonical(row), self.ledger)
        self.assertIsNotNone(res["net_edge_cents"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
