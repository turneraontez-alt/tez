from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger import extract_bridge as extract
from q15_upgrade.challenger.features import FEATURE_NAMES


def _make_rows():
    # Mimic production rows with feature_json carrying champion signals + context.
    rows = []
    for i in range(6):
        rows.append({
            "created_at": 1_700_000_000 + i * 60,
            "checkpoint": "10M" if i % 2 == 0 else "15M",
            "asset": "BTC",
            "ticker": f"KXBTC15M-{i}",
            "official_result": "YES" if i % 2 == 0 else "NO",
            "raw_yes_probability": 0.55 + 0.01 * i,
            "feature_json": {
                "spot": 65000 + i, "target": 65050,
                "seconds_remaining": 600, "volatility_per_min": 0.004,
                "yes_bid": 40, "yes_ask": 42, "no_bid": 56, "no_ask": 58,
                "feature_values": {"momentum": 0.1, "flow": -0.05, "book": 0.02},
            },
        })
    return rows


class ExtractTest(unittest.TestCase):
    def test_export_and_load_roundtrip(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "led.sqlite3")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE predictions (created_at REAL, checkpoint TEXT, asset TEXT, "
                     "ticker TEXT, official_result TEXT, raw_yes_probability REAL, feature_json TEXT, "
                     "model_version TEXT)")
        for r in _make_rows():
            conn.execute(
                "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
                (r["created_at"], r["checkpoint"], r["asset"], r["ticker"], r["official_result"],
                 r["raw_yes_probability"], json.dumps(r["feature_json"]), "v2"),
            )
        # one unresolved row that must be excluded
        conn.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",
                     (1_700_000_999, "10M", "BTC", "KXBTC15M-x", None, 0.5, "{}", "v2"))
        conn.commit()
        conn.close()

        out = os.path.join(d, "dump.jsonl")
        n = extract.export_production_ledger(db, out, model_version="v2")
        self.assertEqual(n, 6)  # unresolved excluded
        rows = extract.load_jsonl(out)
        self.assertEqual(len(rows), 6)
        self.assertIsInstance(rows[0]["feature_json"], dict)

    def test_inspect_feature_json(self):
        rows = _make_rows()
        keys = extract.inspect_feature_json(rows)
        self.assertIn("spot", keys)
        self.assertIn("feature_values", keys)

    def test_rows_to_samples_shapes(self):
        rows = _make_rows()
        ts, feats, y, control, groups = extract.rows_to_samples(rows)
        self.assertEqual(len(ts), 6)
        self.assertEqual(len(y), 6)
        self.assertTrue(all(set(FEATURE_NAMES).issubset(f.keys()) for f in feats))
        self.assertTrue(all(t2 >= t1 for t1, t2 in zip(ts, ts[1:])))  # chronological
        self.assertEqual(set(y), {0, 1})
        self.assertEqual(len(set(groups)), 6)

    def test_rows_to_samples_filters_checkpoint(self):
        rows = _make_rows()
        ts, feats, y, control, groups = extract.rows_to_samples(rows, checkpoint="10M")
        self.assertEqual(len(ts), 3)

    def test_champion_signals_mapped(self):
        ts, feats, y, control, groups = extract.rows_to_samples(_make_rows())
        self.assertAlmostEqual(feats[0]["champ_momentum"], 0.1, places=6)
        self.assertAlmostEqual(feats[0]["champ_flow"], -0.05, places=6)


if __name__ == "__main__":
    unittest.main()
