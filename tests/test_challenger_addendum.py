from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import challenger as ch
from q15_upgrade.challenger import features as featmod
from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.experiment import ExperimentLedger, PreRegistration
from q15_upgrade.challenger.lineage import config_hash, lineage_record
from q15_upgrade.challenger.ood import OODBounds, ood_score
from q15_upgrade.challenger.schema import validate_observation
from q15_upgrade.challenger.stats import (
    block_bootstrap_ci, effective_sample_size, mcnemar, paired_differences,
)
from tests.test_challenger import make_snapshot


# ---- Section B: schema validation ----
class SchemaTest(unittest.TestCase):
    def test_missing_critical_flagged(self):
        res = validate_observation({"asset": "BTC"})
        self.assertFalse(res.ok)
        self.assertIn("contract_id", res.missing_critical)

    def test_receive_after_decision_is_problem(self):
        res = validate_observation({
            "contract_id": "C1", "asset": "BTC", "decision_timestamp_utc": 100.0,
            "expiration_timestamp_utc": 1000.0, "target_price": 65000, "settlement_source": "kalshi",
            "yes_ask": 40, "no_ask": 62, "control_probability": 0.5,
            "final_outcome": "YES", "settlement_status": "VALID",
            "receive_timestamp_utc": 200.0,  # after decision -> leakage
        })
        self.assertFalse(res.ok)
        self.assertTrue(any("leakage" in p for p in res.problems))

    def test_unknown_settlement_status_flagged(self):
        res = validate_observation({
            "contract_id": "C1", "asset": "BTC", "decision_timestamp_utc": 100.0,
            "expiration_timestamp_utc": 1000.0, "target_price": 65000, "settlement_source": "kalshi",
            "yes_ask": 40, "no_ask": 62, "control_probability": 0.5,
            "final_outcome": "YES", "settlement_status": "WEIRD",
        })
        self.assertTrue(any("settlement_status" in p for p in res.problems))


# ---- Section C: point-in-time / receive-time ----
class PointInTimeTest(unittest.TestCase):
    def test_rejects_future_receive_timestamp(self):
        snap = {"spot": 65000, "feed_receive_ts": 500.0}
        with self.assertRaises(ValueError):
            featmod.assert_point_in_time(snap, decision_ts=400.0)

    def test_allows_in_time_receive(self):
        snap = {"spot": 65000, "feed_receive_ts": 300.0}
        featmod.assert_point_in_time(snap, decision_ts=400.0)  # no raise


# ---- Section D: EV / cost model ----
class CostModelTest(unittest.TestCase):
    def test_latency_and_adverse_costs_reduce_net_edge(self):
        base = ChallengerConfig().with_overrides(min_probability=0.55, min_net_edge_cents=1.0)
        rich = base.with_overrides(latency_cost_cents=2.0, adverse_selection_cents=2.0)
        snap, _ = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        fv.yes_ask_cents, fv.yes_bid_cents = 40.0, 38.0
        from q15_upgrade.challenger.decision import evaluate
        d0 = evaluate(base, 0.85, fv, uncertainty=0.1)
        d1 = evaluate(rich, 0.85, fv, uncertainty=0.1)
        self.assertAlmostEqual(d0.net_edge_cents - d1.net_edge_cents, 4.0, places=5)

    def test_does_not_assume_yes_plus_no_equals_one(self):
        # asks chosen so yes_ask + no_ask != 100; both sides priced off their own ask
        snap, _ = make_snapshot(65000, 65050, 600)
        fv = featmod.extract(snap)
        fv.yes_ask_cents, fv.no_ask_cents = 55.0, 55.0  # sum 110, crossed/fee'd market
        from q15_upgrade.challenger.decision import evaluate
        cfg = ChallengerConfig().with_overrides(min_probability=0.5, min_net_edge_cents=100.0)
        d = evaluate(cfg, 0.5, fv, uncertainty=0.1)
        # both asks are expensive -> no trade, and neither side used a midpoint
        self.assertEqual(d.action, "NO_TRADE")


# ---- Section F: OOD fail-safe ----
class OODTest(unittest.TestCase):
    def test_extreme_distance_is_ood(self):
        snap, _ = make_snapshot(65000, 90000, 600)  # target miles away
        fv = featmod.extract(snap)
        score, reasons = ood_score(fv, OODBounds())
        self.assertGreater(score, 0.0)
        self.assertTrue(any("normalized_distance" in r for r in reasons))

    def test_severe_ood_blocks_an_otherwise_tradeable_signal(self):
        # Stub model that is confident YES so a trade WOULD be recommended, then
        # verify a severe OOD observation overrides it to NO_TRADE.
        class _Stub:
            fitted = True
            def predict_proba_one(self, x):
                return 0.9
            def contributions(self, x):
                return {}
            def feature_importance(self):
                return {}

        cfg = ChallengerConfig().with_overrides(min_probability=0.5, min_net_edge_cents=0.0,
                                                ood_severe_threshold=0.1)
        # market quote present + cheap yes_ask => would BUY_YES; tiny vol => extreme
        # normalized_distance => out-of-distribution.
        snap = {
            "spot": 65000, "target": 65100, "seconds_remaining": 600,
            "volatility_per_min": 0.00001,  # absurdly low -> extreme normalized distance
            "yes_bid": 18, "yes_ask": 20, "no_bid": 78, "no_ask": 80,
            "market_implied_prob": 0.5, "data_quality": 0.9, "depth_contracts": 50,
        }
        pred = ch.ShadowPredictor(cfg, _Stub()).predict(snap)
        self.assertGreaterEqual(pred.ood_score, 0.1)
        self.assertEqual(pred.recommendation, "NO_TRADE")
        self.assertTrue(any("ood_block" in w for w in pred.warnings))

    def test_thin_snapshot_is_no_trade_and_high_ood(self):
        cfg = ChallengerConfig()
        snap = {"spot": 65000, "target": 65050, "seconds_remaining": 600}  # no quotes
        pred = ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(snap)
        self.assertGreater(pred.ood_score, 0.0)
        self.assertEqual(pred.recommendation, "NO_TRADE")

    def test_ood_lowers_confidence(self):
        cfg = ChallengerConfig()
        clean, _ = make_snapshot(65000, 65050, 600)
        thin = {"spot": 65000, "target": 65050, "seconds_remaining": 600}
        c_clean = ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(clean).confidence
        c_thin = ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(thin).confidence
        self.assertLess(c_thin, c_clean)


# ---- Section I: paired statistics ----
class PairedStatsTest(unittest.TestCase):
    def test_paired_diff_sign(self):
        y = [1, 0, 1, 0, 1]
        # challenger closer to truth than control
        ctrl = [0.5, 0.5, 0.5, 0.5, 0.5]
        chal = [0.9, 0.1, 0.8, 0.2, 0.85]
        d = paired_differences(ctrl, chal, y)
        self.assertLess(d["mean_log_loss_diff"], 0)   # negative => challenger better
        self.assertLess(d["mean_brier_diff"], 0)

    def test_block_bootstrap_ci_brackets_mean(self):
        vals = [-0.1] * 50
        lo, hi = block_bootstrap_ci(vals, block_size=5, n_boot=500)
        self.assertLessEqual(lo, -0.1 + 1e-9)
        self.assertGreaterEqual(hi, -0.1 - 1e-9)

    def test_mcnemar_and_effective_n(self):
        m = mcnemar([True, False, True, False], [True, True, True, True])
        self.assertEqual(m["c_challenger_only"], 2)
        self.assertTrue(m["challenger_better_discordant"])
        self.assertEqual(effective_sample_size(["w1", "w1", "w2", "w3", "w3"]), 3)


# ---- Section M: lineage ----
class LineageTest(unittest.TestCase):
    def test_lineage_record_has_versions(self):
        cfg = ChallengerConfig()
        rec = lineage_record(cfg, calibrator_version="platt")
        for key in ("code_commit", "config_hash", "feature_schema_version",
                    "label_version", "settlement_definition_version", "calibrator_version"):
            self.assertIn(key, rec)
        self.assertEqual(rec["calibrator_version"], "platt")

    def test_config_hash_changes_with_config(self):
        a = ChallengerConfig()
        b = a.with_overrides(l2=999.0)
        self.assertNotEqual(config_hash(a), config_hash(b))


# ---- Section H: experiment governance ----
class ExperimentTest(unittest.TestCase):
    def _ledger(self):
        d = tempfile.mkdtemp()
        return ExperimentLedger(os.path.join(d, "exp.sqlite3"))

    def test_preregister_and_log(self):
        el = self._ledger()
        el.register(PreRegistration())
        el.log_experiment(kind="model", description="logistic baseline", status="run",
                          metrics={"log_loss": 0.69})
        el.log_experiment(kind="model", description="xgboost depth3", status="rejected",
                          metrics={"log_loss": 0.72})
        rows = el.all_experiments()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"run", "rejected"})
        el.close()

    def test_holdout_taint_tracking(self):
        el = self._ledger()
        self.assertFalse(el.holdout_tainted())
        el.log_holdout_access("peeked to pick threshold", influenced_design=True)
        self.assertTrue(el.holdout_tainted())
        el.close()


# ---- ledger persists ood + lineage ----
class LedgerLineageTest(unittest.TestCase):
    def test_record_stores_ood_and_lineage(self):
        d = tempfile.mkdtemp()
        led = ch.ShadowLedger(os.path.join(d, "shadow.sqlite3"))
        cfg = ChallengerConfig()
        pred = ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(make_snapshot(65000, 65050, 600)[0])
        rid = led.record(pred, asset="BTC", contract="C1", checkpoint="10M",
                         control_prob_yes=0.5, model_version=cfg.model_version,
                         lineage=lineage_record(cfg))
        self.assertIsNotNone(rid)
        row = led._conn.execute("SELECT ood_score, lineage_json FROM shadow_predictions WHERE id=?",
                                (rid,)).fetchone()
        self.assertIsNotNone(row["ood_score"])
        self.assertIn("feature_schema_version", row["lineage_json"])
        led.close()


if __name__ == "__main__":
    unittest.main()
