"""Tests for the updated-review v5 implementation pass.

Covers the data-grounded fixes shipped after running the review against the
live Replit ledgers:

  * 15M alert delivery defaults OFF (coin flip on the record), still recorded;
  * the manipulation standalone alert defaults OFF (anti-predictive on record);
  * the entry-timing experiment (observational): record -> grade -> scoreboard;
  * the prediction_stability shadow signal no longer zeroes itself because the
    0..100 flip score was treated as a 0..1 probability;
  * the isotonic calibration fallback turns the live unconverged-Platt identity
    no-op into a real monotonic calibration (default OFF).
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from q15_upgrade import checkpoint_v95, shadow_signals
from q15_upgrade.ledger_v95 import CHAMPION_WEIGHTS, V95Ledger

NOW = 1_700_000_000.0


class IntervalAlertGateTests(unittest.TestCase):
    def test_15m_alerts_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Q15_V95_15M_ALERTS_ENABLED", None)
            self.assertFalse(checkpoint_v95._interval_alerts_enabled("15M"))
            # The skilled checkpoints always deliver.
            self.assertTrue(checkpoint_v95._interval_alerts_enabled("10M"))
            self.assertTrue(checkpoint_v95._interval_alerts_enabled("7M"))

    def test_15m_alerts_can_be_re_enabled(self):
        with mock.patch.dict(os.environ, {"Q15_V95_15M_ALERTS_ENABLED": "1"}):
            self.assertTrue(checkpoint_v95._interval_alerts_enabled("15M"))


class TimingExperimentMarkTests(unittest.TestCase):
    def test_default_marks_are_13_12_11_minutes(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Q15_V95_TIMING_EXPERIMENT_SECONDS", None)
            self.assertEqual(checkpoint_v95._timing_experiment_marks(), [780, 720, 660])

    def test_marks_parse_and_dedupe_and_bound(self):
        with mock.patch.dict(os.environ, {"Q15_V95_TIMING_EXPERIMENT_SECONDS": "780, 780; 99999, abc, 540"}):
            self.assertEqual(checkpoint_v95._timing_experiment_marks(), [780, 540])

    def test_empty_disables_collection(self):
        with mock.patch.dict(os.environ, {"Q15_V95_TIMING_EXPERIMENT_SECONDS": ""}):
            self.assertEqual(checkpoint_v95._timing_experiment_marks(), [])


class TimingExperimentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _ledger(self):
        return V95Ledger(Path(self.temp.name) / f"timing_{id(self)}.sqlite3")

    def test_record_resolve_and_score(self):
        led = self._ledger()
        # Two marks for two contracts; one contract settles YES, one NO.
        led.record_timing_observation(
            contract="KXBTC-A", mark_seconds=780, asset="BTC", predicted_side="YES",
            yes_probability=0.7, selected_probability=0.7, confidence_grade="B",
            created_at=NOW, close_time=NOW + 780,
        )
        led.record_timing_observation(
            contract="KXBTC-B", mark_seconds=780, asset="BTC", predicted_side="NO",
            yes_probability=0.3, selected_probability=0.7, confidence_grade="C",
            created_at=NOW, close_time=NOW + 780,
        )
        # First write per (contract, mark) wins — a later, flipped call is ignored.
        led.record_timing_observation(
            contract="KXBTC-A", mark_seconds=780, asset="BTC", predicted_side="NO",
            yes_probability=0.1, selected_probability=0.1, confidence_grade="D",
            created_at=NOW + 5, close_time=NOW + 780,
        )
        # Before settlement: pending, no accuracy.
        sb = led.timing_experiment_scoreboard()
        self.assertEqual(sb["by_mark"]["780"]["n"], 0)
        self.assertEqual(sb["by_mark"]["780"]["pending"], 2)

        # Settle: KXBTC-A YES (the YES pick is right), KXBTC-B NO (the NO pick is right).
        led.resolve_ticker("KXBTC-A", "YES", NOW + 800)
        led.resolve_ticker("KXBTC-B", "NO", NOW + 800)
        sb = led.timing_experiment_scoreboard()
        row = sb["by_mark"]["780"]
        self.assertEqual(row["n"], 2)
        self.assertEqual(row["right"], 2)        # both correct
        self.assertEqual(row["pending"], 0)
        self.assertEqual(row["accuracy"], 1.0)
        self.assertAlmostEqual(row["minutes"], 13.0)

    def test_observation_never_touches_official_record(self):
        led = self._ledger()
        led.record_timing_observation(
            contract="KXBTC-A", mark_seconds=720, asset="BTC", predicted_side="YES",
            yes_probability=0.7, selected_probability=0.7, confidence_grade="B",
            created_at=NOW, close_time=NOW + 720,
        )
        led.resolve_ticker("KXBTC-A", "YES", NOW + 800)
        # The official scoreboard is driven by delivered sent_predictions only.
        official = led.official_scoreboard()
        self.assertEqual(official["10M"]["total"]["n"], 0)
        self.assertEqual(official["15M"]["total"]["n"], 0)

    def test_null_predicted_side_row_excluded_not_counted_wrong(self):
        # A timing observation can be recorded with no predicted side. Once the
        # contract settles, an ungradeable (NULL-side) row must be EXCLUDED from
        # the denominator, not silently banked as a loss.
        led = self._ledger()
        led.record_timing_observation(
            contract="KXBTC-G", mark_seconds=660, asset="BTC", predicted_side="YES",
            yes_probability=0.7, selected_probability=0.7, confidence_grade="B",
            created_at=NOW, close_time=NOW + 660,
        )
        led.record_timing_observation(
            contract="KXBTC-N", mark_seconds=660, asset="BTC", predicted_side=None,
            yes_probability=None, selected_probability=None, confidence_grade=None,
            created_at=NOW, close_time=NOW + 660,
        )
        led.resolve_ticker("KXBTC-G", "YES", NOW + 800)  # gradeable, correct
        led.resolve_ticker("KXBTC-N", "NO", NOW + 800)   # NULL side -> ungradeable
        row = led.timing_experiment_scoreboard()["by_mark"]["660"]
        self.assertEqual(row["n"], 1)        # only the gradeable row counts
        self.assertEqual(row["right"], 1)
        self.assertEqual(row["wrong"], 0)    # the NULL-side row is NOT a loss
        self.assertEqual(row["accuracy"], 1.0)


class PredictionStabilitySignalTests(unittest.TestCase):
    def _canonical(self):
        return types.SimpleNamespace(yes_is_higher=True)

    def test_score_on_0_100_scale_no_longer_zeroes_signal(self):
        # flip_risk score is 0..100. A moderate score (30) must leave the signal
        # non-zero; previously 1.0 - 30 clamped to 0 and killed it.
        analysis = {"yes_probability": 0.70, "flip_risk": {"score": 30.0}}
        sig = shadow_signals.compute_signals(analysis, self._canonical())
        # lean = 2*0.7 - 1 = 0.4 ; stability = 1 - 0.30 = 0.70 ; product = 0.28
        self.assertAlmostEqual(sig["prediction_stability"], 0.28, places=4)

    def test_high_flip_score_reduces_stability(self):
        analysis = {"yes_probability": 0.70, "flip_risk": {"score": 90.0}}
        sig = shadow_signals.compute_signals(analysis, self._canonical())
        # stability = 1 - 0.90 = 0.10 ; product = 0.04
        self.assertAlmostEqual(sig["prediction_stability"], 0.04, places=4)

    def test_missing_flip_is_neutral_not_zero(self):
        analysis = {"yes_probability": 0.70}
        sig = shadow_signals.compute_signals(analysis, self._canonical())
        # stability defaults to 0.5 -> 0.4 * 0.5 = 0.20
        self.assertAlmostEqual(sig["prediction_stability"], 0.20, places=4)


class CalibrationIsotonicFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def _ledger(self):
        return V95Ledger(Path(self.temp.name) / f"cal_{id(self)}.sqlite3")

    def _seed_underconfident(self, led, n=120):
        for i in range(n):
            raw = 0.70 if i % 2 == 0 else 0.60
            yes = ((i // 2) % 10 < 9) if i % 2 == 0 else ((i // 2) % 4 < 3)
            tk = f"ISO{i}"
            led.record_prediction(
                ticker=tk, asset="BNB", checkpoint="15M", created_at=NOW, close_time=NOW + 600,
                predicted_side="YES", raw_yes_probability=raw, calibrated_yes_probability=raw,
                challenger_yes_probability=raw, baseline_yes_probability=0.5,
                selected_probability=raw, conservative_probability=raw - 0.02,
                data_quality=0.9, evidence_quality=0.8, trade_quality=0.6,
                trade_decision="WATCH_PRICE", regime="NORMAL",
                features={name: 0.4 for name in CHAMPION_WEIGHTS if name != "intercept"},
                contributions={}, quote={"ask_cents": 55},
            )
            led.resolve_ticker(tk, "YES" if yes else "NO", NOW + i + 1)

    def test_unconverged_identity_when_fallback_off(self):
        # Starve Platt of iterations so it cannot converge -> identity no-op. Opt
        # out of the auto isotonic-fallback + OOS-select to test the legacy path.
        os.environ["Q15_V95_CALIBRATION_MAX_ITERS"] = "1"
        os.environ["Q15_V95_CALIBRATION_ISOTONIC_FALLBACK"] = "0"
        os.environ["Q15_V95_CALIBRATION_OOS_SELECT"] = "0"
        os.environ.pop("Q15_V95_CALIBRATION_ISOTONIC", None)
        led = self._ledger()
        self._seed_underconfident(led)
        res = led.calibrate(0.70, "15M", "BNB")
        self.assertEqual(res.get("fallback"), "identity_unconverged")
        self.assertAlmostEqual(res["probability"], 0.70, places=6)  # raw passes through

    def test_isotonic_fallback_calibrates_unconverged_fit(self):
        os.environ["Q15_V95_CALIBRATION_MAX_ITERS"] = "1"
        os.environ["Q15_V95_CALIBRATION_ISOTONIC_FALLBACK"] = "1"
        os.environ["Q15_V95_CALIBRATION_OOS_SELECT"] = "0"  # test the explicit fallback path
        os.environ.pop("Q15_V95_CALIBRATION_ISOTONIC", None)
        led = self._ledger()
        self._seed_underconfident(led)
        res = led.calibrate(0.70, "15M", "BNB")
        self.assertEqual(res.get("method"), "isotonic")
        self.assertEqual(res.get("reason"), "isotonic_unconverged_fallback")
        # The under-confident raw 0.70 (wins ~0.90) must be pulled upward, not
        # left at identity.
        self.assertGreater(res["probability"], 0.70)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
