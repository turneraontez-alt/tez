"""Tests for the updated-review fixes (Highest / Medium / Polish).

Covers, with deterministic inputs and no real network/clock:
  * H1  evidence-coverage / low-evidence surfacing + the default-OFF alert note
  * H2  canonical cent rounding shared across ledger / performance / money path
  * M1  structural-unavailable fail-closed guard (no thin features pushed)
  * M2  validated SQL identifier path in V95Ledger._ensure_column
  * M3  effective learning-step magnitude + "frozen" observability
  * P1  per-checkpoint dropped_feature_rows counters
  * P2  Wilson CI "excludes 0.5" scoreboard flag
  * P3  websocket data-staleness watchdog helper + health surfacing
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from q15_upgrade import money
from q15_upgrade.checkpoint_v95 import analyse_v95, build_v95_message
from q15_upgrade.ledger_v95 import V95Ledger

import tests.test_q15_v95 as base  # reuse the established snapshot/candle helpers


# --------------------------------------------------------------------------- H2
class MoneyRoundingTests(unittest.TestCase):
    def test_round_cents_bankers_rounding(self):
        # Half-to-even: 0.125 -> 0.12, 0.135 -> 0.14 (no cumulative upward bias).
        self.assertEqual(money.round_cents(0.125), 0.12)
        self.assertEqual(money.round_cents(0.135), 0.14)
        self.assertEqual(money.round_cents(3.3333333), 3.33)

    def test_round_cents_rejects_non_finite_and_missing(self):
        self.assertIsNone(money.round_cents(None))
        self.assertIsNone(money.round_cents(float("nan")))
        self.assertIsNone(money.round_cents(float("inf")))
        self.assertIsNone(money.round_cents("not-a-number"))

    def test_clamp_price_cents_bounds_absolute_prices(self):
        self.assertEqual(money.clamp_price_cents(105.4), 100.0)
        self.assertEqual(money.clamp_price_cents(-3.0), 0.0)
        self.assertEqual(money.clamp_price_cents(41.499), 41.5)

    def test_round_edge_allows_negative_unbounded(self):
        # An edge is a signed delta: negative is legitimate, not clamped to 0.
        self.assertEqual(money.round_edge_cents(-7.256), -7.26)

    def test_is_valid_price_cents(self):
        self.assertTrue(money.is_valid_price_cents(0))
        self.assertTrue(money.is_valid_price_cents(100))
        self.assertFalse(money.is_valid_price_cents(100.01))
        self.assertFalse(money.is_valid_price_cents(float("nan")))

    def test_ledger_and_performance_share_the_same_rounding(self):
        import performance
        import q15_upgrade.ledger_v95 as ledger_mod
        self.assertIs(performance.round_cents, money.round_cents)
        self.assertIs(ledger_mod.round_cents, money.round_cents)


# ----------------------------------------------------------------- H1 + M1 path
class EvidenceAndStructuralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _analyse(self, **canon_kwargs):
        row = base.snapshot()
        canonical = base.build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=base.NOW,
            cached_candles=canon_kwargs.get("cached", base.candles()),
            context=canon_kwargs.get("ctx", base.context()),
            public=canon_kwargs.get("pub", base.public()),
        )
        return analyse_v95(row, canonical, self.ledger)

    def test_evidence_coverage_and_low_evidence_exposed(self):
        result = self._analyse()
        self.assertIn("evidence_coverage", result)
        self.assertIn("low_evidence", result)
        self.assertIn("absent_features", result)
        self.assertIsInstance(result["evidence_coverage"], float)
        self.assertIsInstance(result["absent_features"], list)

    def test_thin_snapshot_is_flagged_low_evidence(self):
        # No public flow/book and minimal candles -> several features fall below
        # the coverage floor, so coverage is low and low_evidence trips.
        thin = self._analyse(cached=base.candles(4), pub={}, ctx={})
        self.assertLess(thin["evidence_coverage"], 0.5)
        self.assertTrue(thin["low_evidence"])
        self.assertTrue(thin["absent_features"])

    def test_missing_feature_contributes_zero_to_logit(self):
        # The core invariant: contribution = weight·value·quality, so a feature
        # with zero quality (its feed produced no data) contributes EXACTLY zero
        # to the model logit — it is treated as missing, never as a neutral 0.0
        # signal that masquerades as support. Every absent feature has quality
        # below the coverage floor.
        thin = self._analyse(cached=base.candles(4), pub={}, ctx={})
        floor = float(os.environ.get("Q15_V95_EVIDENCE_COVERAGE_FLOOR", 0.40))
        for name in thin["absent_features"]:
            self.assertLess(thin["feature_quality"][name], floor)
        zero_q = [n for n, q in thin["feature_quality"].items() if q == 0.0]
        self.assertTrue(zero_q, "expected at least one fully-missing feature in a thin snapshot")
        for name in zero_q:
            self.assertAlmostEqual(thin["contributions"][name], 0.0, places=12)

    def test_low_evidence_note_default_off(self):
        thin = self._analyse(cached=base.candles(4), pub={}, ctx={})
        analyses = {"BNB": thin}
        ranking = [{"asset": "BNB"}]
        os.environ.pop("Q15_V95_LOW_EVIDENCE_FLAG", None)
        msg_off = build_v95_message("10M", analyses, ranking, {})
        self.assertNotIn("Thin evidence", msg_off)
        # Markers must always survive whatever notes we add.
        self.assertIn("V9.5 CHECK", msg_off)

    def test_low_evidence_note_when_enabled(self):
        thin = self._analyse(cached=base.candles(4), pub={}, ctx={})
        analyses = {"BNB": thin}
        ranking = [{"asset": "BNB"}]
        try:
            os.environ["Q15_V95_LOW_EVIDENCE_FLAG"] = "1"
            msg_on = build_v95_message("10M", analyses, ranking, {})
        finally:
            os.environ.pop("Q15_V95_LOW_EVIDENCE_FLAG", None)
        self.assertIn("Thin evidence", msg_on)
        self.assertIn("V9.5 CHECK", msg_on)
        self.assertTrue(msg_on.count("ENTRY") >= 1 or "NO ENTRY" in msg_on)

    def test_structural_unavailable_fails_closed(self):
        # If the structural base fails to load, the analysis must NOT emit a
        # confident number off thin volatility features; it falls back to a
        # prediction-only degraded result.
        row = base.snapshot()
        canonical = base.build_canonical_snapshot(
            row, asset="BNB", checkpoint="10M", now=base.NOW,
            cached_candles=base.candles(), context=base.context(), public=base.public(),
        )
        with patch(
            "q15_upgrade.checkpoint_v95._structural_probability",
            return_value={"available": False, "yes_probability": None},
        ):
            result = analyse_v95(row, canonical, self.ledger)
        self.assertFalse(result["prediction_available"])
        self.assertEqual(result["trade_decision"], "PREDICTION_ONLY")
        self.assertEqual(result["main_blocker"], "structural_model_unavailable")
        self.assertIsNone(result["yes_probability"])
        self.assertTrue(result["low_evidence"])


# ----------------------------------------------------------------- M2 + M3 + P1
class LedgerObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_ensure_column_rejects_unsafe_identifiers(self):
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE t (a INTEGER)")
            with self.assertRaises(ValueError):
                V95Ledger._ensure_column(conn, "t; DROP TABLE t", "b", "b INTEGER")
            with self.assertRaises(ValueError):
                V95Ledger._ensure_column(conn, "t", "b c", "b c INTEGER")
            # DDL that doesn't begin with the column name is rejected too.
            with self.assertRaises(ValueError):
                V95Ledger._ensure_column(conn, "t", "b", "zzz INTEGER")

    def test_ensure_column_adds_valid_column_idempotently(self):
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE t (a INTEGER)")
            V95Ledger._ensure_column(conn, "t", "b", "b INTEGER DEFAULT 0")
            V95Ledger._ensure_column(conn, "t", "b", "b INTEGER DEFAULT 0")  # no-op
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(t)")}
            self.assertIn("b", cols)

    def test_stats_surfaces_new_observability_fields(self):
        stats = self.ledger.status()
        self.assertIn("dropped_feature_rows_by_checkpoint", stats)
        self.assertIn("last_learning_step_magnitude", stats)
        self.assertIn("learning_frozen_results", stats)
        self.assertEqual(stats["learning_frozen_results"], 0)
        self.assertIn("10M", stats["dropped_feature_rows_by_checkpoint"])

    def test_per_checkpoint_dropped_rows_isolated(self):
        # Corrupt feature JSON for a single checkpoint -> only that checkpoint's
        # counter (and the aggregate) move; sibling intervals stay clean.
        self.ledger._dropped_feature_rows_by_checkpoint["10M"] = 0
        self.ledger._dropped_feature_rows_by_checkpoint["15M"] = 0
        self.ledger._dropped_feature_rows += 1
        self.ledger._dropped_feature_rows_by_checkpoint["10M"] += 1
        stats = self.ledger.status()
        self.assertEqual(stats["dropped_feature_rows_by_checkpoint"]["10M"], 1)
        self.assertEqual(stats["dropped_feature_rows_by_checkpoint"]["15M"], 0)
        self.assertGreaterEqual(stats["dropped_feature_rows"], 1)


# ----------------------------------------------------------------------- P2
class WilsonFlagTests(unittest.TestCase):
    @staticmethod
    def _rows(right, n):
        return [{"correct": 1 if i < right else 0, "realized_cents": None} for i in range(n)]

    def test_ci_excludes_half_for_strong_large_sample(self):
        out = V95Ledger._win_loss(self._rows(619, 910))  # ~0.68, big n
        self.assertTrue(out["ci_excludes_half"])
        self.assertFalse(out["low_n"])

    def test_ci_straddles_half_for_tiny_clean_sample(self):
        out = V95Ledger._win_loss(self._rows(3, 3))  # 100% but n=3
        self.assertFalse(out["ci_excludes_half"])  # interval still includes 0.5
        self.assertTrue(out["low_n"])


# ----------------------------------------------------------------------- P3
class WsStalenessTests(unittest.TestCase):
    def test_data_stale_helper(self):
        from spot_ws import _data_stale
        # Disabled watchdog or no message yet -> never stale.
        self.assertFalse(_data_stale(None, 1000.0, 45.0))
        self.assertFalse(_data_stale(900.0, 1000.0, 0.0))
        # Within / beyond the timeout.
        self.assertFalse(_data_stale(990.0, 1000.0, 45.0))
        self.assertTrue(_data_stale(900.0, 1000.0, 45.0))

    def test_health_surfaces_data_staleness(self):
        import spot_ws
        feed = spot_ws.SpotWebSocketFeed()
        # Simulate a provider that connected but went silent well past timeout.
        feed._last_message_at["coinbase"] = 0.0
        with patch.dict(os.environ, {"Q15_SPOT_WS_DATA_TIMEOUT": "45"}):
            health = feed.health()
        self.assertIn("data_stale", health)
        self.assertIn("last_message_age_seconds", health)
        self.assertTrue(health["data_stale"]["coinbase"])
        self.assertEqual(health["data_timeout_seconds"], 45.0)


if __name__ == "__main__":
    unittest.main()
