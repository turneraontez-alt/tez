"""Locks for three review-driven correctness fixes in the v9.5 ledger:

1. PRIMARY-LEARNER BOOST: the heavier 10M sample weight must actually exceed 1.0.
   A legacy ``min(1.0, ...)`` clamp erased the boost for exactly the high-quality
   rows whose base weight already reached 1.0, so 10M learned no faster than 15M.
2. PLATT IDENTITY FALLBACK: an unconverged (untrusted) calibration fit must not
   silently transform live probabilities — it reverts to the identity transform,
   is flagged in the result, and is counted in stats().
3. REGIME SCHEMA GUARD: regime_challenger_weights must carry the same
   CHECK(checkpoint IN ('10M','15M')) as its sibling tables so an accidental 7M
   write fails loudly instead of contaminating regime learning.
"""
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from q15_upgrade.ledger_v95 import V95Ledger


def _mk_ledger():
    led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
    led._cache_enabled = True
    return led


def _record_resolve(led, ticker, official, *, checkpoint="10M", asset="ETH",
                    data_quality=1.0, raw=0.6):
    led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=checkpoint, created_at=time.time(),
        close_time=time.time() + 600, predicted_side="YES",
        raw_yes_probability=raw, calibrated_yes_probability=raw,
        challenger_yes_probability=raw, baseline_yes_probability=0.5,
        selected_probability=raw, conservative_probability=max(0.01, raw - 0.05),
        data_quality=data_quality, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3, "flow": -0.1}, contributions={"momentum": 0.2},
        quote={"ask_cents": 50}, rank=1,
    )
    led.resolve_ticker(ticker, official)


def _last_sample_weight(led, checkpoint):
    with closing(led._connect()) as conn:
        row = conn.execute(
            "SELECT sample_weight FROM checkpoint_challenger_updates "
            "WHERE checkpoint=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (checkpoint,),
        ).fetchone()
    return None if row is None else float(row["sample_weight"])


def _seed(led, n=40, offset=0):
    for i in range(offset, offset + n):
        official = "YES" if i % 3 == 0 else "NO"
        raw = 0.40 + (i % 7) * 0.05
        _record_resolve(led, f"C-{i}", official, raw=raw)


def _seed_calibrated(led):
    """Well-calibrated rows (empirical YES frequency ≈ raw probability) so the
    identity transform is near-optimal and the Newton solve converges quickly."""
    idx = 0
    for raw in (0.45, 0.55, 0.60, 0.65):
        yes = round(raw * 10)
        for k in range(10):
            official = "YES" if k < yes else "NO"
            _record_resolve(led, f"CAL-{idx}", official, raw=raw)
            idx += 1


class PrimaryLearnerBoostTest(unittest.TestCase):
    def test_high_quality_primary_row_keeps_full_boost(self):
        led = _mk_ledger()
        led.primary_learning_weight = 1.25
        led.primary_learning_boost = True
        # data_quality=1.0 -> quality_factor=1.0 -> base sample_weight=1.0; the
        # 10M boost must carry it to 1.25, not clamp it back to 1.0.
        _record_resolve(led, "BOOST-10M", "YES", checkpoint="10M", data_quality=1.0)
        sw = _last_sample_weight(led, "10M")
        self.assertIsNotNone(sw, "a resolved 10M row must record a shadow update")
        self.assertAlmostEqual(sw, 1.25, places=6,
                               msg="primary-learner boost was clamped away")

    def test_legacy_clamp_restores_old_behaviour(self):
        led = _mk_ledger()
        led.primary_learning_weight = 1.25
        led.primary_learning_boost = False  # legacy
        _record_resolve(led, "LEGACY-10M", "YES", checkpoint="10M", data_quality=1.0)
        self.assertAlmostEqual(_last_sample_weight(led, "10M"), 1.0, places=6)

    def test_secondary_checkpoint_is_not_boosted(self):
        led = _mk_ledger()
        led.primary_learning_weight = 1.25
        led.primary_learning_boost = True
        # 15M is not the primary learner -> no boost, base weight stays 1.0.
        _record_resolve(led, "BASE-15M", "YES", checkpoint="15M", data_quality=1.0)
        self.assertAlmostEqual(_last_sample_weight(led, "15M"), 1.0, places=6)


class PlattIdentityFallbackTest(unittest.TestCase):
    def test_unconverged_fit_falls_back_to_identity(self):
        led = _mk_ledger()
        led.calibration_require_converged = True
        led._calibration_max_iters = 1  # force non-convergence on real data
        _seed(led, n=40)
        out = led.calibrate(0.80, "10M", "ETH")
        self.assertTrue(out["active"])
        # Identity transform: the calibrated probability equals the raw input.
        self.assertAlmostEqual(out["probability"], 0.80, places=6)
        self.assertEqual(out["intercept"], 0.0)
        self.assertEqual(out["slope"], 1.0)
        self.assertEqual(out.get("fallback"), "identity_unconverged")
        self.assertEqual(out["reason"], "platt_unconverged_identity")
        self.assertGreaterEqual(led.status()["calibration_unconverged_fallbacks"], 1)

    def test_fallback_disabled_applies_the_unconverged_fit(self):
        led = _mk_ledger()
        led.calibration_require_converged = False
        led._calibration_max_iters = 1
        _seed(led, n=40)
        out = led.calibrate(0.80, "10M", "ETH")
        self.assertTrue(out["active"])
        self.assertIsNone(out.get("fallback"))
        self.assertEqual(out["reason"], "platt_current_version")
        # A real (non-identity) fit moves the probability off the raw input.
        self.assertNotAlmostEqual(out["probability"], 0.80, places=6)

    def test_converged_fit_is_unaffected(self):
        led = _mk_ledger()
        led.calibration_require_converged = True  # full iteration budget (default)
        _seed_calibrated(led)
        fit = led._calibration_fit("10M", "ETH")
        self.assertTrue(fit["converged"], "calibrated data should converge in budget")
        out = led.calibrate(0.80, "10M", "ETH")
        self.assertTrue(out["active"])
        self.assertIsNone(out.get("fallback"))
        self.assertEqual(out["reason"], "platt_current_version")
        self.assertEqual(led.status()["calibration_unconverged_fallbacks"], 0)


class RegimeSchemaGuardTest(unittest.TestCase):
    def test_regime_table_rejects_disallowed_checkpoint(self):
        led = _mk_ledger()
        with closing(led._connect()) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO regime_challenger_weights"
                    "(checkpoint,regime,name,base_value,value,updated_at) "
                    "VALUES('7M','NORMAL','intercept',0.0,0.0,?)",
                    (time.time(),),
                )
                conn.commit()

    def test_regime_table_allows_supported_checkpoints(self):
        led = _mk_ledger()
        with closing(led._connect()) as conn:
            for cp in ("10M", "15M"):
                conn.execute(
                    "INSERT INTO regime_challenger_weights"
                    "(checkpoint,regime,name,base_value,value,updated_at) "
                    "VALUES(?,'NORMAL','intercept',0.0,0.0,?)",
                    (cp, time.time()),
                )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
