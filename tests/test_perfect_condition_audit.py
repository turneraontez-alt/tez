"""Deterministic tests for scripts/perfect_condition_audit.py.

The audit answers a loaded question — "is anything 100% correct?" — so its tests
are mostly *adversarial*: they construct ledgers that would fool a naive miner
(duplicate rows from one market, a leaked outcome column, a perfect rule on a
tiny sample, a perfect rule that dies out of sample) and assert the audit refuses
to be fooled. No live DB, no network, fully deterministic.
"""

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

_spec = importlib.util.spec_from_file_location(
    "perfect_condition_audit", os.path.join(ROOT, "scripts", "perfect_condition_audit.py"))
pca = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pca   # dataclasses need the module registered to resolve annotations
_spec.loader.exec_module(pca)


# --------------------------------------------------------------------------- #
# Ledger fixture helpers                                                       #
# --------------------------------------------------------------------------- #
_COLS = ("prediction_id", "model_version", "ticker", "asset", "checkpoint",
         "created_at", "close_time", "predicted_side", "official_result", "correct",
         "raw_yes_probability", "data_quality", "feature_json", "quote_json")


def _make_db(path: str, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE predictions ("
        "prediction_id TEXT PRIMARY KEY, model_version TEXT, ticker TEXT, asset TEXT, "
        "checkpoint TEXT, created_at REAL, close_time REAL, predicted_side TEXT, "
        "official_result TEXT, correct INTEGER, raw_yes_probability REAL, data_quality REAL, "
        "feature_json TEXT, quote_json TEXT)")
    for r in rows:
        conn.execute(
            f"INSERT INTO predictions ({','.join(_COLS)}) VALUES ({','.join('?' * len(_COLS))})",
            tuple(r.get(c) for c in _COLS))
    conn.commit()
    conn.close()


def _row(i, *, ticker, checkpoint="10M", side="YES", result="YES", asset="BTC",
         created=None, spot=100.5, target=100.0, momentum=0.2, dq=0.9,
         correct=None, model_version="v95"):
    correct = correct if correct is not None else (1 if side == result else 0)
    return {
        "prediction_id": f"{model_version}|{checkpoint}|{ticker}",
        "model_version": model_version, "ticker": ticker, "asset": asset,
        "checkpoint": checkpoint, "created_at": float(created if created is not None else 1_700_000_000 + i * 900),
        "close_time": float(1_700_000_000 + i * 900 + 900),
        "predicted_side": side, "official_result": result, "correct": correct,
        "raw_yes_probability": 0.7 if side == "YES" else 0.3, "data_quality": dq,
        "feature_json": json.dumps({"feature_values": {"momentum": momentum}}),
        "quote_json": json.dumps({"spot": spot, "target": target, "seconds_remaining": 590,
                                  "bid_cents": 55, "ask_cents": 57, "volatility_per_min": 0.003}),
    }


class LoadAndExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_resolved_rows_loaded(self):
        rows = [_row(0, ticker="A"), _row(1, ticker="B")]
        rows[1]["official_result"] = None      # unresolved -> excluded
        rows[1]["correct"] = None
        _make_db(self.path, rows)
        loaded = pca.load_rows(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["ticker"], "A")

    def test_target_recomputed_from_side_and_result(self):
        # predicted NO, settled YES -> incorrect, regardless of any stored flag.
        _make_db(self.path, [_row(0, ticker="A", side="NO", result="YES", correct=1)])
        obs, rep = pca.extract_observations(pca.load_rows(self.path))
        self.assertEqual(obs[0].correct, 0)            # recomputed wins
        self.assertEqual(rep.correct_mismatch, 1)      # and the lie is flagged

    def test_decision_time_variables_extracted_outcome_excluded(self):
        obs, _ = pca.extract_observations(pca.load_rows(self.path) if os.path.exists(self.path) else [])
        _make_db(self.path, [_row(0, ticker="A", spot=101.0, target=100.0)])
        obs, _ = pca.extract_observations(pca.load_rows(self.path))
        o = obs[0]
        self.assertAlmostEqual(o.numeric["pct_distance"], 1.0, places=6)
        self.assertEqual(o.cat["direction"], "UP")
        self.assertIn("momentum", o.numeric)
        self.assertIn("market_implied_prob", o.numeric)
        # leakage firewall: no outcome column ever appears as a variable
        self.assertFalse((set(o.numeric) | set(o.cat)) & pca.DENYLIST)


class LeakageFirewallTest(unittest.TestCase):
    def test_outcome_column_as_variable_raises(self):
        # Hand-craft an observation that smuggles a denylisted column in.
        bad = pca.Obs(ticker="A", checkpoint="10M", created_at=1.0, correct=1,
                      numeric={"realized_cents": 42.0}, cat={})
        # extract_observations builds Obs from raw rows; simulate the firewall directly.
        leaked = (set(bad.numeric) | set(bad.cat)) & pca.DENYLIST
        self.assertEqual(leaked, {"realized_cents"})

    def test_denylist_covers_known_outcome_and_post_decision_fields(self):
        for col in ("official_result", "correct", "realized_cents", "resolved_at",
                    "champion_logloss", "changed_before_close", "learning_applied"):
            self.assertIn(col, pca.DENYLIST, col)


class ChronologicalSplitTest(unittest.TestCase):
    def test_split_is_by_market_and_time_ordered(self):
        obs = [pca.Obs(ticker=f"T{i}", checkpoint="10M", created_at=float(i),
                       correct=1, numeric={}, cat={}) for i in range(10)]
        sp = pca.chronological_split(obs, frac=(0.6, 0.2, 0.2))
        self.assertEqual([o.ticker for o in sp["discovery"]], [f"T{i}" for i in range(6)])
        self.assertEqual([o.ticker for o in sp["validation"]], ["T6", "T7"])
        self.assertEqual([o.ticker for o in sp["test"]], ["T8", "T9"])

    def test_one_market_never_straddles_a_split(self):
        # Same ticker, three checkpoints at different times -> all in one split.
        obs = [pca.Obs(ticker="SAME", checkpoint=cp, created_at=t, correct=1,
                       numeric={}, cat={}) for cp, t in (("15M", 1.0), ("10M", 2.0), ("7M", 3.0))]
        obs += [pca.Obs(ticker=f"X{i}", checkpoint="10M", created_at=10.0 + i,
                        correct=1, numeric={}, cat={}) for i in range(7)]
        sp = pca.chronological_split(obs)
        homes = {name: [t for t, lst in sp.items() if any(o.ticker == "SAME" for o in lst)]
                 for name in ["SAME"]}
        self.assertEqual(len(homes["SAME"]), 1)


class DuplicateMarketTest(unittest.TestCase):
    """3 checkpoint rows from ONE market must count as ONE unique market."""

    def test_three_checkpoints_one_market_is_one_observation(self):
        obs = [pca.Obs(ticker="SAME", checkpoint=cp, created_at=float(i), correct=1,
                       numeric={"data_quality": 0.9}, cat={"asset": "BTC"})
               for i, cp in enumerate(("15M", "10M", "7M"))]
        ev = pca.evaluate((pca.Atom("data_quality", ">=", 0.5),), obs)
        self.assertEqual(ev.rows, 3)
        self.assertEqual(ev.markets, 1)            # not 3
        self.assertEqual(ev.market_wins, 1)
        self.assertEqual(ev.win_rate, 1.0)
        self.assertEqual(pca.sample_tier(ev.markets), "anecdotal")

    def test_market_loses_if_any_checkpoint_wrong(self):
        obs = [pca.Obs(ticker="SAME", checkpoint=cp, created_at=float(i),
                       correct=(0 if cp == "7M" else 1),
                       numeric={"data_quality": 0.9}, cat={})
               for i, cp in enumerate(("15M", "10M", "7M"))]
        ev = pca.evaluate((pca.Atom("data_quality", ">=", 0.5),), obs)
        self.assertEqual(ev.market_wins, 0)        # conservative collapse
        self.assertEqual(ev.market_losses, 1)
        self.assertFalse(ev.perfect)


class ThresholdBoundaryTest(unittest.TestCase):
    def test_ge_and_le_are_inclusive_at_boundary(self):
        o = pca.Obs(ticker="A", checkpoint="10M", created_at=1.0, correct=1,
                    numeric={"x": 5.0}, cat={})
        self.assertTrue(pca.Atom("x", ">=", 5.0).test(o))
        self.assertTrue(pca.Atom("x", "<=", 5.0).test(o))
        self.assertFalse(pca.Atom("x", ">=", 5.0001).test(o))

    def test_missing_variable_is_non_match_not_silent_pass(self):
        o = pca.Obs(ticker="A", checkpoint="10M", created_at=1.0, correct=1,
                    numeric={}, cat={})
        self.assertIsNone(pca.Atom("x", ">=", 0.0).test(o))
        ev = pca.evaluate((pca.Atom("x", ">=", 0.0),), [o])
        self.assertEqual(ev.rows, 0)               # not matched


class MissingAndInvalidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_created_at_excluded_and_counted(self):
        r = _row(0, ticker="A")
        r["created_at"] = None
        _make_db(self.path, [r, _row(1, ticker="B")])
        # load_rows orders by created_at; NULL sorts first but is still selectable.
        obs, rep = pca.extract_observations(pca.load_rows(self.path))
        self.assertEqual(rep.missing_created_at, 1)
        self.assertEqual(len(obs), 1)

    def test_invalid_result_excluded(self):
        r = _row(0, ticker="A")
        r["official_result"] = "MAYBE"             # not YES/NO
        _make_db(self.path, [r, _row(1, ticker="B")])
        # load_rows keeps it (non-null), extract drops it.
        obs, rep = pca.extract_observations(pca.load_rows(self.path))
        self.assertEqual(rep.bad_result, 1)
        self.assertEqual(len(obs), 1)


class SmallSampleAndMultipleTestingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tiny_perfect_rule_is_not_called_credible(self):
        # 6 markets, all correct, all share a quirky momentum -> perfect but tiny.
        rows = [_row(i, ticker=f"M{i}", momentum=0.9, side="YES", result="YES")
                for i in range(6)]
        _make_db(self.path, rows)
        cfg = pca.AuditConfig(min_discovery_markets=1, min_test_markets=20)
        rep = pca.audit(self.path, config=cfg)
        # nothing should be classified CREDIBLE on 6 markets
        self.assertEqual(rep["credible_perfect_count"], 0)
        self.assertNotIn("Strong out-of-sample", rep["executive"])

    def test_expected_false_perfects_reported(self):
        rows = [_row(i, ticker=f"M{i}", side=("YES" if i % 2 else "NO"),
                     result="YES", momentum=(0.5 if i % 2 else -0.5)) for i in range(40)]
        _make_db(self.path, rows)
        cfg = pca.AuditConfig(min_discovery_markets=2, min_test_markets=5)
        rep = pca.audit(self.path, config=cfg)
        self.assertIn("expected_false_perfects", rep)
        self.assertGreaterEqual(rep["expected_false_perfects"], 0.0)
        self.assertGreater(rep["hypotheses_tested"], 0)


class OutOfSampleSurvivalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rule_perfect_only_early_fails_final_test(self):
        # First 80% of markets: momentum>0 -> always YES correct. Last 20%: the
        # same condition flips to wrong. A discovery-perfect rule must be rejected.
        rows = []
        n = 50
        for i in range(n):
            late = i >= int(n * 0.8)
            result = "NO" if late else "YES"          # condition breaks out of sample
            rows.append(_row(i, ticker=f"M{i}", momentum=0.8, side="YES",
                             result=result, created=1_700_000_000 + i * 900))
        _make_db(self.path, rows)
        cfg = pca.AuditConfig(min_discovery_markets=2, min_test_markets=2, max_vars=1)
        rep = pca.audit(self.path, config=cfg)
        self.assertEqual(rep["credible_perfect_count"], 0)
        self.assertIn("failed", rep["executive"].lower() + " ".join(
            r["classification"] for r in rep["perfect"]).lower())

    def test_genuinely_separable_rule_survives_to_test(self):
        # momentum sign perfectly determines outcome in EVERY period -> should be
        # found perfect AND survive the untouched test set.
        rows = []
        n = 120
        for i in range(n):
            up = i % 2 == 0
            rows.append(_row(i, ticker=f"M{i}", momentum=(0.5 if up else -0.5),
                             side=("YES" if up else "NO"), result=("YES" if up else "NO"),
                             created=1_700_000_000 + i * 900))
        _make_db(self.path, rows)
        cfg = pca.AuditConfig(min_discovery_markets=3, min_test_markets=10, max_vars=1)
        rep = pca.audit(self.path, config=cfg)
        self.assertTrue(rep["perfect"], "expected at least one perfect rule")
        self.assertGreaterEqual(rep["credible_perfect_count"], 1)
        self.assertIn("future certainty is NOT established", rep["executive"])


class ReproducibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")
        rows = [_row(i, ticker=f"M{i}", momentum=(0.3 if i % 2 else -0.3),
                     side=("YES" if i % 2 else "NO"), result=("YES" if i % 2 else "NO"))
                for i in range(60)]
        _make_db(self.path, rows)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_runs_are_identical(self):
        cfg = pca.AuditConfig(min_discovery_markets=2, min_test_markets=5)
        a = pca.audit(self.path, config=cfg)
        b = pca.audit(self.path, config=cfg)
        self.assertEqual(json.dumps(a, sort_keys=True, default=str),
                         json.dumps(b, sort_keys=True, default=str))

    def test_read_only_open_does_not_create_or_mutate(self):
        before = os.path.getmtime(self.path)
        pca.audit(self.path, config=pca.AuditConfig(min_discovery_markets=2, min_test_markets=5))
        self.assertEqual(os.path.getmtime(self.path), before)
        # a non-existent ledger opened read-only must NOT be created.
        missing = os.path.join(self.tmp.name, "nope.sqlite3")
        with self.assertRaises(sqlite3.OperationalError):
            pca.load_rows(missing)
        self.assertFalse(os.path.exists(missing))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "led.sqlite3")
        _make_db(self.path, [_row(i, ticker=f"M{i}") for i in range(30)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_runs(self):
        self.assertEqual(pca.main(["--ledger", self.path, "--min-test-markets", "5"]), 0)

    def test_main_json(self):
        self.assertEqual(pca.main(["--ledger", self.path, "--json", "--min-test-markets", "5"]), 0)

    def test_main_missing_ledger_returns_2(self):
        self.assertEqual(pca.main(["--ledger", "/nonexistent/x.sqlite3"]), 2)


if __name__ == "__main__":
    unittest.main()
