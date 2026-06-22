"""Behaviour-identical hot-path caches for the v9.5 ledger (calibrate /
pattern_similarity / challenger_weights).

These functions ran per asset per ~1s cycle but only change when results are
resolved or challenger weights are written. They are now memoized against a
monotonic data version bumped on those two mutations. These tests lock the two
properties that make the cache safe on the money path:

1. EQUIVALENCE: a cache hit returns byte-identical output to a fresh recompute.
2. INVALIDATION: resolving new rows refits (no stale cache served).
"""
import tempfile
import time
import unittest

from q15_upgrade.ledger_v95 import V95Ledger


def _mk_ledger():
    led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
    led._cache_enabled = True  # don't depend on the ambient env
    # These tests pin the parametric Platt-fit cache; opt out of the new auto
    # isotonic-fallback / OOS-self-select so the applied method is deterministically
    # Platt (with intercept/slope) regardless of the seed's OOS outcome.
    led._calibration_isotonic_fallback = False
    led._calibration_oos_select = False
    return led


def _record_resolve(led, ticker, side, raw, official, *, checkpoint="10M", asset="ETH"):
    led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=checkpoint, created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=raw, calibrated_yes_probability=raw,
        challenger_yes_probability=raw, baseline_yes_probability=0.5,
        selected_probability=raw, conservative_probability=max(0.01, raw - 0.05),
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3, "flow": -0.1}, contributions={"momentum": 0.2},
        quote={"ask_cents": 50}, rank=1,
    )
    led.resolve_ticker(ticker, official)


def _seed(led, n=40, offset=0):
    """Seed n resolved (10M, ETH) rows with mixed outcomes (winners + losers)."""
    for i in range(offset, offset + n):
        side = "YES" if i % 2 == 0 else "NO"
        raw = 0.40 + (i % 7) * 0.05
        official = "YES" if i % 3 == 0 else "NO"
        _record_resolve(led, f"C-{i}", side, raw, official)


class CalibrateCacheTest(unittest.TestCase):
    def test_cache_hit_equals_recompute(self):
        led = _mk_ledger()
        _seed(led)
        probes = [0.2, 0.5, 0.73, 0.9]
        cached = [led.calibrate(p, "10M", "ETH") for p in probes]    # compute + populate
        hit = [led.calibrate(p, "10M", "ETH") for p in probes]       # cache hits
        led._cache_enabled = False
        recompute = [led.calibrate(p, "10M", "ETH") for p in probes]  # fresh each time
        self.assertEqual(cached, hit)
        self.assertEqual(cached, recompute)
        self.assertTrue(cached[0]["active"])

    def test_fit_is_independent_of_raw_probability(self):
        led = _mk_ledger()
        _seed(led)
        a = led.calibrate(0.2, "10M", "ETH")
        b = led.calibrate(0.85, "10M", "ETH")
        # Same fit (cached), different applied probability.
        self.assertEqual(a["intercept"], b["intercept"])
        self.assertEqual(a["slope"], b["slope"])
        self.assertNotEqual(a["probability"], b["probability"])

    def test_resolution_invalidates_the_fit(self):
        led = _mk_ledger()
        _seed(led, n=40)
        fit_a = led.calibrate(0.7, "10M", "ETH")
        self.assertTrue(fit_a["active"])
        # Resolve 25 more strongly-consistent rows -> the fit must move.
        for i in range(100, 125):
            _record_resolve(led, f"E-{i}", "YES", 0.55, "YES")
        fit_b = led.calibrate(0.7, "10M", "ETH")
        self.assertNotEqual(
            (fit_a["intercept"], fit_a["slope"], fit_a["rows"]),
            (fit_b["intercept"], fit_b["slope"], fit_b["rows"]),
            "cache served a stale Platt fit after new resolutions",
        )

    def test_insufficient_rows_inactive_and_identical(self):
        led = _mk_ledger()
        _seed(led, n=5)  # below minimum_calibration_rows
        cached = led.calibrate(0.6, "10M", "ETH")
        led._cache_enabled = False
        fresh = led.calibrate(0.6, "10M", "ETH")
        self.assertFalse(cached["active"])
        self.assertEqual(cached, fresh)


class PatternSimilarityCacheTest(unittest.TestCase):
    def test_cache_hit_equals_recompute(self):
        led = _mk_ledger()
        _seed(led)
        feats = {"momentum": 0.3, "flow": -0.1}
        for side in ("YES", "NO"):
            cached = led.pattern_similarity(feats, side, "10M")
            hit = led.pattern_similarity(feats, side, "10M")
            led._cache_enabled = False
            recompute = led.pattern_similarity(feats, side, "10M")
            led._cache_enabled = True
            self.assertEqual(cached, hit)
            self.assertEqual(cached, recompute)
        self.assertTrue(cached["active"])

    def test_resolution_invalidates_centroids(self):
        led = _mk_ledger()
        _seed(led, n=40)
        feats = {"momentum": 0.3, "flow": -0.1}
        a = led.pattern_similarity(feats, "YES", "10M")
        for i in range(200, 230):
            _record_resolve(led, f"P-{i}", "YES", 0.6, "YES")
        b = led.pattern_similarity(feats, "YES", "10M")
        self.assertNotEqual(a["resolved"], b["resolved"])


class ChallengerWeightsCacheTest(unittest.TestCase):
    def test_cache_hit_equals_recompute(self):
        led = _mk_ledger()
        cached = led.challenger_weights("10M", None)
        hit = led.challenger_weights("10M", None)
        led._cache_enabled = False
        recompute = led.challenger_weights("10M", None)
        self.assertEqual(cached, hit)
        self.assertEqual(cached, recompute)

    def test_returns_a_fresh_copy(self):
        led = _mk_ledger()
        a = led.challenger_weights("10M", None)
        a["intercept"] = 999.0  # caller mutates its copy
        b = led.challenger_weights("10M", None)  # cache hit
        self.assertNotEqual(b.get("intercept"), 999.0)


if __name__ == "__main__":
    unittest.main()
