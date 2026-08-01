from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import challenger as ch
from q15_upgrade.challenger import features as featmod
from q15_upgrade.challenger.calibration import IsotonicCalibrator, PlattCalibrator
from q15_upgrade.challenger import calibration as calibration_mod
from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.decision import evaluate
from q15_upgrade.challenger.mathx import normal_cdf
from q15_upgrade.challenger.models import LogisticRegression
from q15_upgrade.challenger import models as models_mod
from q15_upgrade.challenger.validation import purged_walk_forward


def make_snapshot(spot, target, seconds, vol=0.004, rng=None, noise=0.0):
    """A decision-time snapshot whose true P(Yes)=Phi(-normalized_distance)."""
    minutes = seconds / 60.0
    denom = spot * vol * math.sqrt(minutes)
    nd = (target - spot) / denom
    true_p = normal_cdf(-nd)
    market = min(0.99, max(0.01, true_p + noise))
    yes_mid = market * 100.0
    spread = 2.0
    return {
        "spot": spot, "target": target, "seconds_remaining": seconds,
        "volatility_per_min": vol, "data_quality": 0.8,
        "yes_bid": yes_mid - spread / 2, "yes_ask": yes_mid + spread / 2,
        "no_bid": (100 - yes_mid) - spread / 2, "no_ask": (100 - yes_mid) + spread / 2,
        "depth_contracts": 50,
        "feature_values": {"momentum": 0.1, "flow": -0.05, "book": 0.0},
    }, true_p


def synth_dataset(n=200, seed=1):
    rng = random.Random(seed)
    ts, feats, y = [], [], []
    t0 = 1_700_000_000.0
    for i in range(n):
        spot = 65000 + rng.uniform(-500, 500)
        target = spot + rng.uniform(-120, 120)
        seconds = rng.choice([900, 600, 420])
        vol = 0.004
        snap, true_p = make_snapshot(spot, target, seconds, vol, noise=rng.uniform(-0.03, 0.03))
        # realised outcome consistent with the structural probability
        z = rng.gauss(0, 1)
        price_T = spot * math.exp(vol * math.sqrt(seconds / 60.0) * z)
        label = 1 if price_T >= target else 0
        fv = featmod.extract(snap)
        ts.append(t0 + i * 60.0)
        feats.append(fv.details)
        y.append(label)
    return ts, feats, y


class FeaturesTest(unittest.TestCase):
    def test_extract_shapes_and_market_prob(self):
        snap, true_p = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        self.assertEqual(len(fv.values), len(featmod.FEATURE_NAMES))
        self.assertEqual(len(fv.coverage), len(featmod.FEATURE_NAMES))
        self.assertIsNotNone(fv.market_yes_prob)
        self.assertAlmostEqual(fv.market_yes_prob, true_p, delta=0.05)

    def test_missing_fields_lower_coverage(self):
        fv = featmod.extract({"spot": 65000})  # almost everything missing
        self.assertLess(fv.coverage_fraction, 0.5)

    def test_leakage_guard_rejects_outcome_keys(self):
        with self.assertRaises(ValueError):
            featmod.assert_no_leakage(["spot", "official_result"])
        # clean keys pass
        featmod.assert_no_leakage(["spot", "target", "yes_ask"])

    def test_normalized_distance_sign(self):
        # target above spot -> normalized_distance > 0 -> P(Yes) < 0.5
        fv = featmod.extract(make_snapshot(65000, 65100, 600)[0])
        self.assertGreater(fv.details["normalized_distance"], 0)
        self.assertLess(fv.market_yes_prob, 0.5)


class ModelTest(unittest.TestCase):
    def test_logistic_learns_separable(self):
        ts, feats, y = synth_dataset(300, seed=2)
        X = [ch.ordered_vector(f) for f in feats]
        m = LogisticRegression(l2=1.0, lr=0.1, max_iter=400).fit(X, y)
        preds = m.predict_proba(X)
        # in-sample log-loss should beat a constant 0.5 predictor
        ll = -sum(yi * math.log(p) + (1 - yi) * math.log(1 - p) for p, yi in zip(preds, y)) / len(y)
        self.assertLess(ll, math.log(2))
        self.assertTrue(all(0.01 <= p <= 0.99 for p in preds))

    def test_contributions_present(self):
        ts, feats, y = synth_dataset(120, seed=3)
        X = [ch.ordered_vector(f) for f in feats]
        m = LogisticRegression().fit(X, y)
        c = m.contributions(X[0])
        self.assertEqual(set(c.keys()), set(featmod.FEATURE_NAMES))

    def test_numpy_fit_matches_dependency_free_fallback(self):
        if models_mod._np is None:
            self.skipTest("NumPy fast path is not installed")
        _, feats, y = synth_dataset(120, seed=19)
        X = [ch.ordered_vector(f) for f in feats]
        fast = LogisticRegression(l2=1.0, lr=0.08, max_iter=80).fit(X, y)
        with mock.patch.object(models_mod, "_np", None):
            fallback = LogisticRegression(l2=1.0, lr=0.08, max_iter=80).fit(X, y)
        for actual, expected in zip(fast.predict_proba(X), fallback.predict_proba(X)):
            self.assertAlmostEqual(actual, expected, places=11)


class CalibrationTest(unittest.TestCase):
    def test_platt_improves_biased_probs(self):
        rng = random.Random(0)
        # systematically overconfident probabilities
        y = [1 if rng.random() < 0.6 else 0 for _ in range(400)]
        raw = [min(0.99, max(0.01, 0.6 + (0.3 if yi else -0.3))) for yi in y]
        cal = PlattCalibrator(l2=1.0).fit(raw, y)
        self.assertTrue(cal.fitted)
        out = [cal.transform(p) for p in raw]
        self.assertTrue(all(0.01 <= o <= 0.99 for o in out))

    def test_isotonic_monotone(self):
        y = [0, 0, 1, 0, 1, 1, 1]
        p = [0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.9]
        cal = IsotonicCalibrator().fit(p, y)
        outs = [cal.transform(x) for x in [0.05, 0.25, 0.5, 0.95]]
        for a, b in zip(outs, outs[1:]):
            self.assertLessEqual(a, b + 1e-9)

    def test_numpy_platt_matches_dependency_free_fallback(self):
        if calibration_mod._np is None:
            self.skipTest("NumPy fast path is not installed")
        probs = [0.05 + 0.9 * (i / 99.0) for i in range(100)]
        labels = [int((i * 17) % 31 < 15) for i in range(100)]
        fast = PlattCalibrator(l2=2.0, lr=0.07, max_iter=80).fit(probs, labels)
        with mock.patch.object(calibration_mod, "_np", None):
            fallback = PlattCalibrator(l2=2.0, lr=0.07, max_iter=80).fit(probs, labels)
        self.assertAlmostEqual(fast.a, fallback.a, places=11)
        self.assertAlmostEqual(fast.b, fallback.b, places=11)


class ValidationTest(unittest.TestCase):
    def test_purged_walk_forward_respects_embargo_and_purge(self):
        ts = [1000.0 + i * 60 for i in range(60)]  # 60 rows, 60s apart
        folds = purged_walk_forward(ts, n_splits=3, embargo_seconds=900, horizon_seconds=900)
        self.assertTrue(folds)
        for fold in folds:
            test_start_ts = ts[fold.test_idx[0]]
            # embargo: no train row within 900s before test start
            for i in fold.train_idx + fold.val_idx:
                self.assertLessEqual(ts[i], test_start_ts - 900)
            # purge: no train label window overlaps any test window
            test_windows = [(ts[i], ts[i] + 900) for i in fold.test_idx]
            for i in fold.train_idx + fold.val_idx:
                w0, w1 = ts[i], ts[i] + 900
                for a, b in test_windows:
                    self.assertTrue(w1 <= a or w0 >= b)
            # chronological: all train before all test
            self.assertLess(max(fold.train_idx + fold.val_idx), min(fold.test_idx))

    def test_rejects_unsorted(self):
        with self.assertRaises(ValueError):
            purged_walk_forward([3.0, 1.0, 2.0], n_splits=1)


class DecisionTest(unittest.TestCase):
    def test_no_trade_when_edge_too_small(self):
        cfg = ChallengerConfig()
        snap, _ = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        dec = evaluate(cfg, fv.market_yes_prob, fv, uncertainty=0.2)  # model == market -> ~0 edge
        self.assertEqual(dec.action, "NO_TRADE")

    def test_trade_when_strong_edge(self):
        cfg = ChallengerConfig().with_overrides(min_probability=0.55, min_net_edge_cents=3.0)
        snap, _ = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        # model very confident YES while yes_ask is cheap
        fv.yes_ask_cents = 40.0
        fv.yes_bid_cents = 38.0
        dec = evaluate(cfg, 0.85, fv, uncertainty=0.1)
        self.assertEqual(dec.action, "BUY_YES")
        self.assertGreater(dec.net_edge_cents, 3.0)
        self.assertGreater(dec.hypothetical_size_fraction, 0.0)

    def test_risk_gate_blocks_wide_spread(self):
        cfg = ChallengerConfig().with_overrides(min_probability=0.55, min_net_edge_cents=1.0,
                                                max_spread_cents=3.0)
        snap, _ = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        fv.spread_cents = 20.0
        fv.yes_ask_cents = 40.0
        dec = evaluate(cfg, 0.9, fv, uncertainty=0.1)
        self.assertEqual(dec.action, "NO_TRADE")
        self.assertIn("spread_too_wide", dec.warnings)


class PredictorTest(unittest.TestCase):
    def test_eight_outputs_and_cold_start(self):
        cfg = ChallengerConfig()
        pred = ch.ShadowPredictor(cfg, ch.make_model(cfg))  # unfitted
        out = pred.predict(make_snapshot(65000, 65050, 600)[0])
        self.assertTrue(0 <= out.prob_yes <= 1)
        self.assertAlmostEqual(out.prob_yes + out.prob_no, 1.0, places=5)
        self.assertTrue(0 <= out.confidence <= 1)
        self.assertIn(out.recommendation, {"BUY_YES", "BUY_NO", "NO_TRADE"})
        self.assertIn("model_untrained_cold_start", out.warnings)
        # cold start defers to market price
        self.assertAlmostEqual(out.prob_yes, out.market_yes_prob, delta=0.02)

    def test_trained_predictor_outputs(self):
        ts, feats, y = synth_dataset(300, seed=5)
        cfg = ChallengerConfig().with_overrides(min_train_rows=50)
        predictor, info = ch.train_predictor(ts, feats, y, cfg)
        self.assertTrue(info["fitted"])
        out = predictor.predict(make_snapshot(65000, 65100, 600)[0])
        self.assertTrue(len(out.top_factors) > 0)


class HarnessTest(unittest.TestCase):
    def test_walk_forward_evaluate_reports_metrics(self):
        ts, feats, y = synth_dataset(400, seed=7)
        cfg = ChallengerConfig().with_overrides(n_splits=4)
        res = ch.walk_forward_evaluate(ts, feats, y, cfg)
        self.assertTrue(res["ok"])
        self.assertIn("challenger", res)
        self.assertIn("market_only", res)
        for key in ("brier", "log_loss", "ece", "accuracy"):
            self.assertIn(key, res["challenger"])

    def test_insufficient_data_returns_unfitted(self):
        ts, feats, y = synth_dataset(10, seed=8)
        cfg = ChallengerConfig().with_overrides(min_train_rows=200)
        predictor, info = ch.train_predictor(ts, feats, y, cfg)
        self.assertFalse(info["fitted"])


class LedgerTest(unittest.TestCase):
    def _ledger(self):
        d = tempfile.mkdtemp()
        return ch.ShadowLedger(os.path.join(d, "shadow.sqlite3"))

    def test_record_resolve_score(self):
        led = self._ledger()
        cfg = ChallengerConfig().with_overrides(min_train_rows=50)
        ts, feats, y = synth_dataset(300, seed=11)
        predictor, info = ch.train_predictor(ts, feats, y, cfg)
        self.assertTrue(info["fitted"])
        # record 40 contracts then resolve them
        for i in range(40):
            snap, true_p = make_snapshot(65000, 65000 + (i - 20) * 5, 600)
            pred = predictor.predict(snap)
            cid = f"BTC-{i}"
            led.record(pred, asset="BTC", contract=cid, checkpoint="10M",
                       control_prob_yes=pred.market_yes_prob, model_version=cfg.model_version)
            outcome = "YES" if true_p >= 0.5 else "NO"
            led.resolve(cid, "10M", outcome, model_version=cfg.model_version)
        sb = led.scoreboard(model_version=cfg.model_version)
        self.assertEqual(sb["resolved"], 40)
        self.assertIn("challenger", sb)
        self.assertIn("brier", sb["challenger"])
        led.close()

    def test_record_is_immutable_on_duplicate(self):
        led = self._ledger()
        cfg = ChallengerConfig()
        pred = ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(make_snapshot(65000, 65050, 600)[0])
        id1 = led.record(pred, asset="BTC", contract="C1", checkpoint="10M",
                         control_prob_yes=0.5, model_version=cfg.model_version)
        id2 = led.record(pred, asset="BTC", contract="C1", checkpoint="10M",
                         control_prob_yes=0.5, model_version=cfg.model_version)
        self.assertIsNotNone(id1)
        self.assertIsNone(id2)  # duplicate ignored
        led.close()


class PromotionSeamTest(unittest.TestCase):
    def test_off_returns_champion(self):
        cfg = ChallengerConfig()  # as_primary False by default
        p = ch.primary_probability(make_snapshot(65000, 65050, 600)[0], champion_prob_yes=0.42,
                                   predictor=None, config=cfg)
        self.assertEqual(p, 0.42)

    def test_on_but_untrained_falls_back_to_champion(self):
        cfg = ChallengerConfig().with_overrides(as_primary=True)
        predictor = ch.ShadowPredictor(cfg, ch.make_model(cfg))  # unfitted
        p = ch.primary_probability(make_snapshot(65000, 65050, 600)[0], champion_prob_yes=0.42,
                                   predictor=predictor, config=cfg)
        self.assertEqual(p, 0.42)

    def test_on_and_trained_uses_challenger(self):
        ts, feats, y = synth_dataset(300, seed=13)
        cfg = ChallengerConfig().with_overrides(as_primary=True, min_train_rows=50)
        predictor, info = ch.train_predictor(ts, feats, y, cfg)
        self.assertTrue(info["fitted"])
        snap = make_snapshot(65000, 65100, 600)[0]
        p = ch.primary_probability(snap, champion_prob_yes=0.42, predictor=predictor, config=cfg)
        self.assertNotEqual(p, 0.42)  # now driven by the challenger
        self.assertTrue(0.0 <= p <= 1.0)


if __name__ == "__main__":
    unittest.main()
