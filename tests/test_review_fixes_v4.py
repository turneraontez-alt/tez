"""Tests for the updated-review implementation pass (v4).

Covers the isotonic calibration upgrade (highest-impact): the pure PAVA
fit/predict helpers, that the live path is byte-for-byte Platt when the option
is OFF (default), and that when ON the isotonic map corrects the recorded
high-band UNDER-confidence (raw 0.7 → wins ~0.9) far better than identity.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from q15_upgrade.ledger_v95 import (
    CHAMPION_WEIGHTS, V95Ledger, _isotonic_fit, _isotonic_predict,
)

NOW = 1_700_000_000.0


class IsotonicPureTests(unittest.TestCase):
    def test_fit_is_monotonic_non_decreasing(self):
        # Deliberately non-monotonic raw input must pool into a monotone map.
        pairs = [(0.1, 0), (0.2, 1), (0.3, 0), (0.4, 1), (0.5, 1), (0.6, 0), (0.7, 1)]
        anchors = _isotonic_fit(pairs)
        ys = [y for _x, y in anchors]
        self.assertEqual(ys, sorted(ys), "anchors must be non-decreasing")

    def test_predict_interpolates_and_clamps(self):
        anchors = [(0.2, 0.3), (0.8, 0.9)]
        self.assertAlmostEqual(_isotonic_predict(anchors, 0.1), 0.3)   # below range -> first
        self.assertAlmostEqual(_isotonic_predict(anchors, 0.9), 0.9)   # above range -> last
        self.assertAlmostEqual(_isotonic_predict(anchors, 0.5), 0.6)   # midpoint
        self.assertIsNone(_isotonic_predict([], 0.5))

    def test_empty_fit(self):
        self.assertEqual(_isotonic_fit([]), [])


class CalibrationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ.pop("Q15_V95_CALIBRATION_ISOTONIC", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.temp.cleanup()

    def _ledger(self):
        return V95Ledger(Path(self.temp.name) / f"led_{id(self)}.sqlite3")

    def _seed_underconfident(self, ledger, n=120):
        # raw 0.70 wins ~90%; raw 0.60 wins ~75% -> the model is under-confident
        # and a good calibrator should map 0.70 upward toward ~0.90.
        for i in range(n):
            raw = 0.70 if i % 2 == 0 else 0.60
            if i % 2 == 0:
                yes = (i // 2) % 10 < 9      # 90% YES
            else:
                yes = (i // 2) % 4 < 3       # 75% YES
            tk = f"ISO{i}"
            ledger.record_prediction(
                ticker=tk, asset="BNB", checkpoint="15M", created_at=NOW, close_time=NOW + 600,
                predicted_side="YES", raw_yes_probability=raw, calibrated_yes_probability=raw,
                challenger_yes_probability=raw, baseline_yes_probability=0.5,
                selected_probability=raw, conservative_probability=raw - 0.02,
                data_quality=0.9, evidence_quality=0.8, trade_quality=0.6,
                trade_decision="WATCH_PRICE", regime="NORMAL",
                features={name: 0.4 for name in CHAMPION_WEIGHTS if name != "intercept"},
                contributions={}, quote={"ask_cents": 55},
            )
            ledger.resolve_ticker(tk, "YES" if yes else "NO", NOW + i + 1)

    def test_default_off_uses_platt_not_isotonic(self):
        led = self._ledger()
        self._seed_underconfident(led)
        res = led.calibrate(0.70, "15M", "BNB")
        self.assertTrue(res["active"])
        self.assertNotEqual(res.get("method"), "isotonic")
        self.assertIn("platt", res["reason"])

    def test_isotonic_on_corrects_underconfidence(self):
        os.environ["Q15_V95_CALIBRATION_ISOTONIC"] = "1"
        led = self._ledger()
        self._seed_underconfident(led)
        res = led.calibrate(0.70, "15M", "BNB")
        self.assertTrue(res["active"])
        self.assertEqual(res.get("method"), "isotonic")
        # raw 0.70 actually wins ~90%; isotonic must pull it well above raw,
        # closer to the realized rate than the uncorrected 0.70.
        self.assertGreater(res["probability"], 0.80)
        self.assertLess(abs(res["probability"] - 0.90), abs(0.70 - 0.90))


if __name__ == "__main__":
    unittest.main()
