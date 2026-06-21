"""Repair coverage for the Shadow-vs-Your-System window grading.

The deployed report showed Your System as 0W-0L | N/A everywhere and Shadow with
only one 15M pick (ranks #2/#3 and the 10M/7M intervals missing). Root cause: the
production canonical stores ``close_time = now + seconds_remaining`` — a per-asset,
per-cycle estimate — and the shadow ledger bucketed grading cases by the EXACT
close second (``round(close)``). That jitter shattered each window into singleton
cases, so only rank #1 of one asset survived and whole intervals vanished.

These tests pin the repair:
  * Cases bucket by the 15-minute window (nearest boundary), so every asset in a
    window groups together → ranks #1/#2/#3 at all three intervals, even with
    realistic per-asset/per-cycle close jitter.
  * A 15M case never blocks 10M/7M (independent storage rows + independent cases).
  * Your System delivery status is recorded: SENT counts in the visible record;
    a failed send is kept as background DELIVERY_FAILED (out of the totals) with the
    exact error; neither is silently lost.
  * Restarting (reopening the SQLite ledger) preserves records and does not
    duplicate or re-grade them.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.ledger import ShadowLedger, _window_id
from q15_upgrade.challenger.runner import ShadowRunner

ASSETS = ["BTC", "ETH", "SOL", "DOGE", "XRP"]
PROBS = {"BTC": 0.72, "ETH": 0.61, "SOL": 0.44, "DOGE": 0.83, "XRP": 0.55}
OUTCOMES = {"BTC": "YES", "ETH": "NO", "SOL": "NO", "DOGE": "YES", "XRP": "YES"}
TRUE_CLOSE = 1_700_000_900.0  # exact 15-min boundary in epoch seconds
CHECKPOINTS = (("15M", 900), ("10M", 600), ("7M", 420))


def _cfg(tmp, **kw):
    base = dict(enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
                model_version="sim", refit_every=0, min_train_rows=5)
    base.update(kw)
    return ChallengerConfig().with_overrides(**base)


def _run_full_window(r, *, deliver=True, jitter=True, seed=1):
    """Drive one full window: observe every asset at 15M/10M/7M with realistic
    close jitter, optionally mark each delivered, then settle all three intervals."""
    rng = random.Random(seed)
    for cp, secs in CHECKPOINTS:
        for a in ASSETS:
            now = TRUE_CLOSE - secs + (rng.uniform(-1.5, 1.5) if jitter else 0.0)
            seconds = secs + (rng.uniform(-1.5, 1.5) if jitter else 0.0)
            r.observe(ticker=f"KX{a}", asset=a, checkpoint=cp, created_at=now,
                      close_time=now + seconds, control_prob_yes=PROBS[a],
                      features={"momentum": 0.1, "flow": 0.0, "book": 0.0},
                      quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
            if deliver:
                r.mark_native_sent(f"KX{a}", cp)
    for a in ASSETS:
        for cp, _ in CHECKPOINTS:
            r.resolve(f"KX{a}", cp, OUTCOMES[a], resolved_at=TRUE_CLOSE + 1)


class WindowBucketingTest(unittest.TestCase):
    def test_window_id_collapses_jitter_to_one_boundary(self):
        # Closes within a few seconds of the same true boundary share a window id.
        self.assertEqual(_window_id(TRUE_CLOSE - 1.4), _window_id(TRUE_CLOSE + 1.4))
        # Distinct windows stay distinct.
        self.assertNotEqual(_window_id(TRUE_CLOSE), _window_id(TRUE_CLOSE + 900))
        self.assertIsNone(_window_id(None))

    def test_full_window_produces_all_ranks_at_all_intervals(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        _run_full_window(r, deliver=True)

        rk = r.ranked()
        self.assertEqual(rk["n_cases"], 3, "exactly one case per interval (15M/10M/7M)")

        win = r.ledger.latest_window_cases(model_version="sim")
        self.assertEqual(set(win["checkpoints"]), {"15M", "10M", "7M"})
        for cp in ("15M", "10M", "7M"):
            picks = win["checkpoints"][cp]
            self.assertEqual(len(picks["challenger"]), 3, f"{cp} shadow needs 3 ranks")
            self.assertEqual(len(picks["native"]), 3, f"{cp} Your System needs 3 ranks")

        # All-time per-interval/per-rank records populate for BOTH systems.
        rbc = r.ledger.ranked_by_checkpoint(model_version="sim")
        for cp in ("15M", "10M", "7M"):
            for system in ("challenger", "native"):
                total = rbc["by_checkpoint"][cp][system]["total"]
                self.assertEqual(total["correct"] + total["wrong"], 3,
                                 f"{cp}/{system} should grade 3 ranks")
                self.assertIsNotNone(total["accuracy"])

    def test_report_renders_three_ranks_each_interval(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        _run_full_window(r, deliver=True)
        msg = r.report_message().replace("<pre>", "").replace("</pre>", "")
        # Each interval block carries #1/#2/#3 with a Shadow and a Yours side.
        for n in ("#1 Shadow:", "#2 Shadow:", "#3 Shadow:"):
            self.assertIn(n, msg)
        self.assertIn("| Yours:", msg)
        # Your System is no longer universally N/A: at least one real W/L shows.
        self.assertTrue(any(tok in msg for tok in ("W–", "W-")))


class IntervalIndependenceTest(unittest.TestCase):
    def test_15m_does_not_block_10m_or_7m(self):
        # The same contract recorded at all three checkpoints yields three rows and
        # three independent cases — a 15M never suppresses the 10M/7M prediction.
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        for cp, secs in CHECKPOINTS:
            now = TRUE_CLOSE - secs
            r.observe(ticker="KXBTC", asset="BTC", checkpoint=cp, created_at=now,
                      close_time=now + secs, control_prob_yes=0.7,
                      features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        rows = r.ledger._conn.execute(
            "SELECT DISTINCT checkpoint FROM shadow_predictions WHERE model_version='sim'"
        ).fetchall()
        self.assertEqual({row["checkpoint"] for row in rows}, {"15M", "10M", "7M"})
        for cp, _ in CHECKPOINTS:
            r.resolve("KXBTC", cp, "YES", resolved_at=TRUE_CLOSE + 1)
        rbc = r.ledger.ranked_by_checkpoint(model_version="sim")
        for cp in ("15M", "10M", "7M"):
            # Each interval independently grades exactly one rank-1 result (the lone
            # BTC pick) — proof a 15M does not block / absorb the 10M or 7M case.
            rank1 = rbc["by_checkpoint"][cp]["challenger"]["rank1"]
            self.assertEqual(rank1["correct"] + rank1["wrong"], 1,
                             f"{cp} must grade independently of the others")


class DeliveryStatusTest(unittest.TestCase):
    def test_sent_counts_failed_stays_background_with_error(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # Two assets in one 10M window: one delivered, one delivery-failed.
        for a, prob in (("BTC", 0.8), ("ETH", 0.3)):
            r.observe(ticker=f"KX{a}", asset=a, checkpoint="10M", created_at=TRUE_CLOSE - 600,
                      close_time=TRUE_CLOSE, control_prob_yes=prob,
                      features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        self.assertTrue(r.mark_native_sent("KXBTC", "10M"))
        self.assertTrue(r.mark_native_delivery_failed("KXETH", "10M", "HTTP 429: rate limited"))
        for a in ("BTC", "ETH"):
            r.resolve(f"KX{a}", "10M", OUTCOMES[a], resolved_at=TRUE_CLOSE + 1)

        # Visible native record contains ONLY the delivered (BTC) pick.
        rbc = r.ledger.ranked_by_checkpoint(model_version="sim", native_sent_only=True)
        native_total = rbc["by_checkpoint"]["10M"]["native"]["total"]
        self.assertEqual(native_total["correct"] + native_total["wrong"], 1)
        # Shadow (read-only) still grades both.
        chal_total = rbc["by_checkpoint"]["10M"]["challenger"]["total"]
        self.assertEqual(chal_total["correct"] + chal_total["wrong"], 2)

        # The failed pick is preserved as background with the exact error.
        audit = r.delivery_audit()
        self.assertEqual(audit["SENT"], 1)
        self.assertEqual(audit["DELIVERY_FAILED"], 1)
        row = r.ledger._conn.execute(
            "SELECT native_sent, native_delivery_status, native_delivery_error "
            "FROM shadow_predictions WHERE contract='KXETH' AND checkpoint='10M'"
        ).fetchone()
        self.assertEqual(row["native_sent"], 0)
        self.assertEqual(row["native_delivery_status"], "DELIVERY_FAILED")
        self.assertIn("429", row["native_delivery_error"])

    def test_failed_then_delivered_promotes_and_clears_error(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        r.observe(ticker="KXBTC", asset="BTC", checkpoint="10M", created_at=TRUE_CLOSE - 600,
                  close_time=TRUE_CLOSE, control_prob_yes=0.8,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        self.assertTrue(r.mark_native_delivery_failed("KXBTC", "10M", "timeout"))
        # A later successful delivery (safe-retry policy) promotes it to SENT.
        self.assertTrue(r.mark_native_sent("KXBTC", "10M"))
        row = r.ledger._conn.execute(
            "SELECT native_sent, native_delivery_status, native_delivery_error "
            "FROM shadow_predictions WHERE contract='KXBTC' AND checkpoint='10M'"
        ).fetchone()
        self.assertEqual(row["native_sent"], 1)
        self.assertEqual(row["native_delivery_status"], "SENT")
        self.assertIsNone(row["native_delivery_error"])


class RestartPersistenceTest(unittest.TestCase):
    def test_reopen_preserves_records_without_duplication_or_regrade(self):
        tmp = tempfile.mkdtemp()
        cfg = _cfg(tmp)
        r = ShadowRunner(cfg)
        _run_full_window(r, deliver=True)
        before = r.ranked()
        n_rows = r.ledger._conn.execute(
            "SELECT COUNT(*) c FROM shadow_predictions WHERE model_version='sim'"
        ).fetchone()["c"]

        # "Restart": reopen the same SQLite file with a fresh ledger/runner.
        r.ledger.close()
        r2 = ShadowRunner(cfg)
        after = r2.ranked()
        n_rows2 = r2.ledger._conn.execute(
            "SELECT COUNT(*) c FROM shadow_predictions WHERE model_version='sim'"
        ).fetchone()["c"]

        self.assertEqual(n_rows, n_rows2, "restart must not add or drop rows")
        self.assertEqual(before["n_cases"], after["n_cases"])
        self.assertEqual(before["challenger"]["overall"], after["challenger"]["overall"])
        self.assertEqual(before["native"]["overall"], after["native"]["overall"])

        # Re-resolving an already-settled contract is a no-op (no double grade).
        self.assertFalse(
            r2.ledger.resolve("KXBTC", "15M", "YES", model_version="sim"),
            "an already-resolved (contract, checkpoint) must not regrade",
        )

    def test_migration_adds_delivery_columns_to_legacy_db(self):
        import sqlite3
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE shadow_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "model_version TEXT, contract TEXT, checkpoint TEXT, official_result TEXT, "
                     "native_sent INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO shadow_predictions (model_version, contract, checkpoint) "
                     "VALUES ('sim','C1','10M')")
        conn.commit(); conn.close()
        led = ShadowLedger(path)  # __init__ migrates
        cols = {row["name"] for row in led._conn.execute("PRAGMA table_info(shadow_predictions)")}
        for c in ("native_delivery_status", "native_delivery_error", "native_delivery_at"):
            self.assertIn(c, cols)
        # The legacy row still works with the new failure path.
        self.assertTrue(led.mark_native_delivery_failed("C1", "10M", "boom", model_version="sim"))


if __name__ == "__main__":
    unittest.main()
