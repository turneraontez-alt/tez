"""The scripts/challenger_eval.py OOS evaluation tool (read-only, on-Repl helper)."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig

# Load the script module by path (it lives in scripts/, not an importable package).
_spec = importlib.util.spec_from_file_location(
    "challenger_eval", os.path.join(ROOT, "scripts", "challenger_eval.py"))
challenger_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(challenger_eval)


def _make_ledger(path: str, n: int = 150) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE predictions (prediction_id TEXT, model_version TEXT, checkpoint TEXT, "
        "created_at REAL, official_result TEXT, raw_yes_probability REAL, ticker TEXT, "
        "asset TEXT, feature_json TEXT, quote_json TEXT)")
    checkpoints = ("15M", "10M", "7M")
    for i in range(n):
        cp = checkpoints[i % 3]
        # A learnable signal: spot above target -> YES. The market quote is held
        # roughly flat so the market-only baseline cannot trivially capture it.
        above = (i % 2 == 0)
        spot = 100.0 + (0.5 if above else -0.5)
        target = 100.0
        y = "YES" if above else "NO"
        champ_prob = 0.68 if above else 0.32
        feature_json = json.dumps({"feature_values": {
            "momentum": 0.2 if above else -0.2, "flow": 0.1 if above else -0.1,
            "book": 0.0, "wick": 0.0, "context": 0.05 if above else -0.05,
            "threshold_interaction": 0.3 if above else -0.3,
            "exchange_consensus": 0.1, "derivatives": 0.0, "absorption": 0.0}})
        quote_json = json.dumps({
            "bid_cents": 49, "ask_cents": 51, "spread_cents": 2,
            "spot": spot, "target": target, "seconds_remaining": 590,
            "volatility_per_min": 0.003, "depth_contracts": 20, "data_quality": 0.9})
        conn.execute(
            "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"v95|{cp}|T{i}", "v95", cp, 1_700_000_000.0 + i * 1000.0, y,
             champ_prob, f"KXBTC-{cp}-{i}", "BTC", feature_json, quote_json))
    conn.commit()
    conn.close()


class EvaluateLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "v95.sqlite3")
        _make_ledger(self.ledger)
        # Thin-data config so the walk-forward actually runs in a unit test.
        self.cfg = ChallengerConfig().with_overrides(min_train_rows=20, n_splits=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_has_pooled_and_per_checkpoint(self):
        rep = challenger_eval.evaluate_ledger(self.ledger, config=self.cfg)
        self.assertEqual(rep["resolved_rows"], 150)
        # Pooled slice ran a walk-forward and computed a champion bar.
        self.assertTrue(rep["pooled"]["walk_forward"]["ok"])
        self.assertIsNotNone(rep["pooled"]["champion"])
        self.assertIn("challenger", rep["pooled"]["walk_forward"])
        self.assertIn("market_only", rep["pooled"]["walk_forward"])
        # All three checkpoints are represented and counted.
        self.assertEqual(set(rep["per_checkpoint"]), {"15M", "10M", "7M"})
        self.assertEqual(sum(s["n"] for s in rep["per_checkpoint"].values()), 150)

    def test_features_reach_the_model_via_quote_json(self):
        # The signal lives in spot/target (carried in quote_json). If the bridge
        # read it, the challenger's OOS log-loss should be finite and better than
        # the flat market-only baseline on this constructed-separable data.
        rep = challenger_eval.evaluate_ledger(self.ledger, config=self.cfg)
        wf = rep["pooled"]["walk_forward"]
        self.assertLess(wf["challenger"]["log_loss"], wf["market_only"]["log_loss"])

    def test_empty_ledger_reports_note(self):
        empty = os.path.join(self.tmp.name, "empty.sqlite3")
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE predictions (prediction_id TEXT, official_result TEXT, "
                     "created_at REAL, feature_json TEXT, model_version TEXT, checkpoint TEXT)")
        conn.commit()
        conn.close()
        rep = challenger_eval.evaluate_ledger(empty, config=self.cfg)
        self.assertEqual(rep["resolved_rows"], 0)
        self.assertIn("note", rep)

    def test_main_runs_and_prints(self):
        rc = challenger_eval.main(["--ledger", self.ledger, "--min-rows", "20", "--splits", "3"])
        self.assertEqual(rc, 0)

    def test_main_missing_ledger_returns_2(self):
        rc = challenger_eval.main(["--ledger", "/nonexistent/path.sqlite3"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
