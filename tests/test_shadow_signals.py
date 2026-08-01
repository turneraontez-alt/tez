"""Tests for the background shadow-signal experiment (q15_upgrade/shadow_signals.py
+ the ledger persistence and A/B grading).

Deterministic, no network/clock. Verifies:
  * the six signals compute from existing analysis/canonical data
  * record_prediction persists them in their isolated column (idempotent, fresh
    insert only) and resolved_shadow_signal_rows reads them back ordered
  * the out-of-sample A/B detects a genuinely predictive signal and does NOT
    flag a pure-noise one
  * report rendering + default-OFF gating + the policy/endpoint wrapper
"""
from __future__ import annotations

import os
import random
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from q15_upgrade import shadow_signals
from q15_upgrade.ledger_v95 import CHAMPION_WEIGHTS, V95Ledger

import tests.test_q15_v95 as base


class ComputeSignalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _analysis_and_canonical(self):
        row = base.snapshot()
        canonical = base.build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=base.NOW,
            cached_candles=base.candles(), context=base.context(), public=base.public(),
        )
        from q15_upgrade.checkpoint_v95 import analyse_v95
        return analyse_v95(row, canonical, self.ledger), canonical

    def test_all_signals_present_and_bounded(self):
        analysis, canonical = self._analysis_and_canonical()
        cfg = shadow_signals.SignalConfig.from_env()
        signals = shadow_signals.compute_signals(analysis, canonical, cfg)
        self.assertEqual(set(signals), set(shadow_signals.SIGNAL_NAMES))
        self.assertIn("coinflip_fade", signals)
        for name, value in signals.items():
            self.assertIsInstance(value, float)
            self.assertGreaterEqual(value, -1.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_coinflip_fade_de_confidences_only_in_the_band(self):
        # YES-oriented de-confidence localised to the near-coin-flip band: a YES
        # lean inside the band yields a NEGATIVE signal (pull back toward 0.5), a NO
        # lean yields POSITIVE, a perfect coin flip yields 0 (no lean to fade), and
        # confident calls outside the band yield EXACTLY 0 (left alone), so the
        # 17/17 high-conviction record can never be touched.
        class _Bare:
            yes_is_higher = True
            candles = ()

        def fade(p):
            return shadow_signals.compute_signals({"yes_probability": p}, _Bare())["coinflip_fade"]

        self.assertAlmostEqual(fade(0.5), 0.0, places=6)           # exact coin flip: no lean
        self.assertLess(fade(0.55), 0.0)                           # YES lean -> fade down
        self.assertGreater(fade(0.45), 0.0)                        # NO lean  -> fade up
        self.assertAlmostEqual(fade(0.55), -fade(0.45), places=6)  # symmetric about 0.5
        self.assertGreater(abs(fade(0.55)), abs(fade(0.59)))       # stronger nearer the flip
        # Outside the band (|p-0.5| >= _COINFLIP_FADE_BAND) the correction is zero.
        for p in (0.61, 0.75, 0.90, 0.39, 0.10):
            self.assertAlmostEqual(fade(p), 0.0, places=6, msg=f"p={p} must be untouched")

    def test_compute_degrades_without_exception(self):
        # Missing analysis fields -> zeros, never raises.
        class _Bare:
            yes_is_higher = True
            candles = ()
        signals = shadow_signals.compute_signals({}, _Bare())
        self.assertEqual(set(signals), set(shadow_signals.SIGNAL_NAMES))
        for value in signals.values():
            self.assertEqual(value, 0.0)

    def test_order_flow_persistence_not_dead_on_candle_gap(self):
        # Review repair: flow present but no recent candle-returns to confirm
        # drift must carry a damped flow lean, not collapse to 0.0.
        class _Bare:
            yes_is_higher = True
            candles = ()
        analysis = {"yes_probability": 0.5, "feature_values": {"flow": 0.8}}
        signals = shadow_signals.compute_signals(analysis, _Bare())
        self.assertAlmostEqual(signals["order_flow_persistence"], 0.4, places=6)

    def test_prediction_stability_neutral_when_flip_risk_absent(self):
        # Review repair: a MISSING flip-risk score must read as neutral (0.5),
        # not "perfectly stable" (1.0) which would amplify the lean to full.
        class _Bare:
            yes_is_higher = True
            candles = ()
        analysis = {"yes_probability": 0.9}  # lean = +0.8, no flip_risk key
        signals = shadow_signals.compute_signals(analysis, _Bare())
        self.assertAlmostEqual(signals["prediction_stability"], 0.4, places=6)

    def test_regime_transition_deconfidences_when_inputs_absent(self):
        # Review repair: no regime inputs at all → unknown (transition_prob 0.5),
        # so the lean is moderately de-confidenced rather than left at full.
        class _Bare:
            yes_is_higher = True
            candles = ()
        analysis = {"yes_probability": 0.9}  # lean = +0.8, no regime/threshold/vol
        signals = shadow_signals.compute_signals(analysis, _Bare())
        self.assertAlmostEqual(signals["regime_transition"], 0.4, places=6)

    def test_entropy_signal_shrinks_with_noise(self):
        # A clean one-directional candle series -> low entropy -> stronger
        # entropy_noise magnitude than an oscillating (noisy) series.
        row = base.snapshot()
        trend = base.build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=base.NOW,
            cached_candles=base.candles(oscillate=False), context=base.context(), public=base.public(),
        )
        noisy = base.build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=base.NOW,
            cached_candles=base.candles(oscillate=True), context=base.context(), public=base.public(),
        )
        from q15_upgrade.checkpoint_v95 import analyse_v95
        a_trend = analyse_v95(row, trend, self.ledger)
        a_noisy = analyse_v95(row, noisy, self.ledger)
        s_trend = shadow_signals.compute_signals(a_trend, trend)
        s_noisy = shadow_signals.compute_signals(a_noisy, noisy)
        self.assertGreaterEqual(abs(s_trend["entropy_noise"]), abs(s_noisy["entropy_noise"]))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _record(self, ticker, signals, yes=0.62, side="YES"):
        return self.ledger.record_prediction(
            ticker=ticker, asset="BNB", checkpoint="10M", created_at=base.NOW, close_time=base.NOW + 600,
            predicted_side=side, raw_yes_probability=yes, calibrated_yes_probability=yes,
            challenger_yes_probability=yes, baseline_yes_probability=0.6,
            selected_probability=yes, conservative_probability=0.58, data_quality=0.8,
            evidence_quality=0.7, trade_quality=0.6, trade_decision="WATCH_PRICE", regime="NORMAL",
            features={n: 0.4 for n in CHAMPION_WEIGHTS if n != "intercept"},
            contributions={}, quote={"ask_cents": 55}, shadow_signals=signals,
        )

    def test_signals_round_trip_and_isolated_column(self):
        sig = {n: 0.5 for n in shadow_signals.SIGNAL_NAMES}
        pid, inserted = self._record("SIGT1", sig)
        self.assertTrue(inserted)
        self.ledger.resolve_ticker("SIGT1", "YES", base.NOW + 700)
        rows = self.ledger.resolved_shadow_signal_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], 1)
        self.assertAlmostEqual(rows[0]["champion_yes"], 0.62, places=6)
        self.assertEqual(rows[0]["signals"]["book_resiliency"], 0.5)

    def test_dedup_does_not_overwrite_signals(self):
        self._record("SIGT2", {n: 0.5 for n in shadow_signals.SIGNAL_NAMES})
        # Second insert is a no-op; must not overwrite the locked signal row.
        _, inserted = self._record("SIGT2", {n: -0.9 for n in shadow_signals.SIGNAL_NAMES})
        self.assertFalse(inserted)
        self.ledger.resolve_ticker("SIGT2", "YES", base.NOW + 700)
        rows = self.ledger.resolved_shadow_signal_rows()
        self.assertEqual(rows[0]["signals"]["book_resiliency"], 0.5)

    def test_unresolved_rows_excluded(self):
        self._record("SIGT3", {n: 0.5 for n in shadow_signals.SIGNAL_NAMES})
        self.assertEqual(self.ledger.resolved_shadow_signal_rows(), [])


class EvaluationTests(unittest.TestCase):
    def _rows(self, signal_fn, n=200, seed=7):
        rng = random.Random(seed)
        rows = []
        for _ in range(n):
            outcome = 1 if rng.random() < 0.5 else 0
            champion_yes = 0.5  # uninformative champion, so signal can add value
            signals = {name: 0.0 for name in shadow_signals.SIGNAL_NAMES}
            signals["order_flow_persistence"] = signal_fn(outcome, rng)
            rows.append({"champion_yes": champion_yes, "outcome": outcome, "signals": signals})
        return rows

    def test_predictive_signal_is_flagged_improving(self):
        # Signal strongly aligned with the outcome must show a positive
        # out-of-sample Brier reduction and significance.
        rows = self._rows(lambda y, rng: (1.0 if y else -1.0) * (0.6 + 0.4 * rng.random()))
        cfg = shadow_signals.SignalConfig.from_env()
        scores = shadow_signals.evaluate(rows, cfg)
        score = scores["order_flow_persistence"]
        self.assertFalse(score.insufficient)
        self.assertGreater(score.brier_reduction, 0.0)
        self.assertTrue(score.improves)

    def test_noise_signal_not_flagged(self):
        rows = self._rows(lambda y, rng: rng.uniform(-1.0, 1.0))
        cfg = shadow_signals.SignalConfig.from_env()
        scores = shadow_signals.evaluate(rows, cfg)
        self.assertFalse(scores["order_flow_persistence"].improves)

    def test_insufficient_rows_marked(self):
        rows = self._rows(lambda y, rng: 0.0, n=5)
        scores = shadow_signals.evaluate(rows, shadow_signals.SignalConfig.from_env())
        self.assertTrue(scores["order_flow_persistence"].insufficient)

    def test_numpy_offset_fit_matches_dependency_free_fallback(self):
        if shadow_signals._np is None:
            self.skipTest("NumPy fast path is not installed")
        offsets = [(-1.5 + i * 0.03) for i in range(100)]
        signals = [((i * 13) % 29 - 14) / 14.0 for i in range(100)]
        labels = [int((i * 17) % 31 < 15) for i in range(100)]
        fast = shadow_signals._fit_logistic_offset(offsets, signals, labels, iters=80)
        with mock.patch.object(shadow_signals, "_np", None):
            fallback = shadow_signals._fit_logistic_offset(offsets, signals, labels, iters=80)
        for actual, expected in zip(fast, fallback):
            self.assertAlmostEqual(actual, expected, places=11)


class ReportingAndGatingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()
        os.environ.pop("Q15_V95_SHADOW_SIGNALS_ENABLED", None)

    def test_report_lines_render_for_usable_scores(self):
        rng = random.Random(3)
        rows = []
        for _ in range(120):
            y = 1 if rng.random() < 0.5 else 0
            rows.append({
                "champion_yes": 0.5, "outcome": y,
                "signals": {n: ((1.0 if y else -1.0) * 0.8 if n == "book_resiliency" else 0.0)
                            for n in shadow_signals.SIGNAL_NAMES},
            })
        scores = shadow_signals.evaluate(rows, shadow_signals.SignalConfig.from_env())
        lines = shadow_signals.build_report_lines(scores)
        self.assertTrue(any("Experimental signals" in ln for ln in lines))

    def test_experiment_default_on_and_endpoint_shape(self):
        # Collection now defaults ON (observational A/B only — never feeds the
        # live probability), so the evidence can actually accumulate.
        os.environ.pop("Q15_V95_SHADOW_SIGNALS_ENABLED", None)
        exp = self.ledger.shadow_signal_experiment()
        self.assertIn("signals", exp)
        self.assertTrue(exp["enabled"])
        self.assertEqual(exp["rows_considered"], 0)
        self.assertEqual(set(exp["signals"]), set(shadow_signals.SIGNAL_NAMES))

    def test_experiment_can_be_disabled_via_env(self):
        os.environ["Q15_V95_SHADOW_SIGNALS_ENABLED"] = "0"
        try:
            self.assertFalse(shadow_signals.SignalConfig.from_env().enabled)
        finally:
            os.environ.pop("Q15_V95_SHADOW_SIGNALS_ENABLED", None)


if __name__ == "__main__":
    unittest.main()
