"""TTL memoization for the display-only V95Ledger.status().

The ~1s main loop calls status() every cycle, but it runs several unbounded
full-table scans over the (unpruned) predictions table — the dominant
"slower over time" cost. status() is DISPLAY-ONLY (the live alert's "pushed
accuracy" line and /api/health), so it is now TTL-memoized: it recomputes at
most once per _STATUS_CACHE_TTL_SECONDS rather than every cycle.

These tests lock the properties that make that cache safe:

1. BEHAVIOUR-NEUTRAL: a cache hit returns the SAME values a fresh uncached
   recompute would over the same seeded rows.
2. CACHED WITHIN TTL: new rows inserted within the window do not change the
   returned status; after the window it refreshes.
3. COPY ISOLATION: the caller gets a copy, so mutating it cannot corrupt the
   cached object.
4. INDEXES: the four performance indexes are created (idempotently) on init.
"""
import sqlite3
import tempfile
import time
import unittest

from q15_upgrade import ledger_v95
from q15_upgrade.ledger_v95 import V95Ledger


def _mk_ledger():
    led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
    led._cache_enabled = True  # don't depend on the ambient env
    return led


def _record_resolve(led, ticker, side, raw, official, *, checkpoint="10M",
                    asset="ETH", pushed=False, resolve=True):
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
    if pushed:
        led.mark_pushed(ticker, checkpoint)
    if resolve:
        led.resolve_ticker(ticker, official)


def _seed(led, n=60, offset=0):
    """Seed n predictions across checkpoints/outcomes, some pushed, most resolved.

    Exercises every status() count branch: total / resolved / per-checkpoint /
    learning_applied / pushed_by_checkpoint.
    """
    checkpoints = ("15M", "10M", "7M")
    for i in range(offset, offset + n):
        side = "YES" if i % 2 == 0 else "NO"
        raw = 0.40 + (i % 7) * 0.05
        official = "YES" if i % 3 == 0 else "NO"
        cp = checkpoints[i % 3]
        # Leave a few rows unresolved so total != resolved.
        resolve = (i % 11 != 0)
        _record_resolve(
            led, f"C-{i}", side, raw, official,
            checkpoint=cp, pushed=(i % 4 == 0), resolve=resolve,
        )


def _recompute_fresh(led):
    """Force status() to recompute from the DB (bypass the TTL cache)."""
    led._status_cache = None
    led._status_cache_at = 0.0
    return led.status()


class StatusCacheBehaviourNeutralTest(unittest.TestCase):
    def test_cached_equals_fresh_recompute(self):
        led = _mk_ledger()
        _seed(led)
        cached = led.status()          # compute + populate cache
        hit = led.status()             # served from cache (within TTL)
        fresh = _recompute_fresh(led)  # uncached recompute over identical rows
        self.assertEqual(cached, hit)
        self.assertEqual(cached, fresh)
        # Sanity: the seed actually populated the scanned counts.
        self.assertGreater(cached["unique_predictions"], 0)
        self.assertGreater(cached["unique_resolved"], 0)
        self.assertTrue(cached["pushed_by_checkpoint"])

    def test_cached_value_is_a_known_snapshot(self):
        led = _mk_ledger()
        _seed(led, n=60)
        st = led.status()
        # Cross-check a couple of the scanned aggregates against an independent
        # query, proving the cached dict carries the real counts (not placeholders).
        with sqlite3.connect(led.path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE model_version=?",
                (ledger_v95.MODEL_VERSION,),
            ).fetchone()[0]
            resolved = conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE model_version=? "
                "AND official_result IS NOT NULL",
                (ledger_v95.MODEL_VERSION,),
            ).fetchone()[0]
        self.assertEqual(st["unique_predictions"], total)
        self.assertEqual(st["unique_resolved"], resolved)


class StatusCacheTtlTest(unittest.TestCase):
    def test_within_ttl_does_not_see_new_rows(self):
        led = _mk_ledger()
        _seed(led, n=40)
        first = led.status()
        before = first["unique_predictions"]
        # Insert + resolve new rows AFTER the cache is warm.
        for i in range(500, 520):
            _record_resolve(led, f"N-{i}", "YES", 0.55, "YES")
        within = led.status()  # still inside the TTL window -> cached
        self.assertEqual(
            within["unique_predictions"], before,
            "status() refreshed inside the TTL window (cache not serving)",
        )

    def test_after_ttl_refreshes(self):
        led = _mk_ledger()
        _seed(led, n=40)
        first = led.status()
        before = first["unique_predictions"]
        for i in range(600, 620):
            _record_resolve(led, f"M-{i}", "YES", 0.55, "YES")
        # Expire the cache by back-dating its stamp past the TTL.
        led._status_cache_at -= (ledger_v95._STATUS_CACHE_TTL_SECONDS + 1.0)
        after = led.status()
        self.assertEqual(
            after["unique_predictions"], before + 20,
            "status() did not refresh after the TTL elapsed",
        )

    def test_zero_ttl_always_refreshes(self):
        led = _mk_ledger()
        _seed(led, n=20)
        original_ttl = ledger_v95._STATUS_CACHE_TTL_SECONDS
        ledger_v95._STATUS_CACHE_TTL_SECONDS = 0.0
        try:
            before = led.status()["unique_predictions"]
            for i in range(700, 710):
                _record_resolve(led, f"Z-{i}", "NO", 0.45, "NO")
            after = led.status()["unique_predictions"]
        finally:
            ledger_v95._STATUS_CACHE_TTL_SECONDS = original_ttl
        self.assertEqual(after, before + 10)


class StatusCacheCopyIsolationTest(unittest.TestCase):
    def test_caller_mutation_does_not_corrupt_cache(self):
        led = _mk_ledger()
        _seed(led, n=30)
        a = led.status()
        a["unique_predictions"] = -999
        a["pushed_by_checkpoint"]["BOGUS"] = {"settled": 1}  # mutate a nested dict
        b = led.status()  # cache hit
        self.assertNotEqual(b["unique_predictions"], -999)
        self.assertNotIn("BOGUS", b["pushed_by_checkpoint"])


class StatusCacheIndexTest(unittest.TestCase):
    EXPECTED_INDEXES = (
        "idx_v95_predictions_ticker_unresolved",
        "idx_v95_timing_experiment_contract",
        "idx_v95_flip_decisions_contract",
        "idx_v95_predictions_resolved_mv",
    )

    def test_indexes_created_on_init(self):
        led = _mk_ledger()
        with sqlite3.connect(led.path) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        for idx in self.EXPECTED_INDEXES:
            self.assertIn(idx, names, f"missing performance index {idx!r}")

    def test_predictions_partial_index_via_pragma(self):
        led = _mk_ledger()
        with sqlite3.connect(led.path) as conn:
            pred_indexes = {
                row[1]  # name column of index_list
                for row in conn.execute("PRAGMA index_list('predictions')")
            }
        self.assertIn("idx_v95_predictions_ticker_unresolved", pred_indexes)
        self.assertIn("idx_v95_predictions_resolved_mv", pred_indexes)


if __name__ == "__main__":
    unittest.main()
