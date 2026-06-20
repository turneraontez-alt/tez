"""Locks the batched rolling-window read (recent_observations_for_pairs).

pre_enrich_all read the rolling evidence window with one Postgres round-trip per
asset. recent_observations_for_pairs collapses those into ONE round-trip via a
UNION ALL of the exact per-pair query, so every group is identical to a
standalone recent_observations() call.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from q15_upgrade.checkpoint_v91 import CheckpointPolicyV91


class _DisabledStore:
    enabled = False


class _FakePgStore:
    """Minimal Postgres-like store: makes persistence.pg True, records queries."""
    enabled = True

    def __init__(self, rows=None):
        self.queries = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        return True

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        return list(self._rows)


def _obs(key, contract, asset, bucket, side, p_yes=0.2):
    return {"observation_key": key, "contract_key": contract, "asset": asset,
            "bucket": bucket, "observed_at": float(bucket), "side": side, "p_yes": p_yes}


class RollingBatchSqliteEquivalence(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V91_ROLLING_WINDOW"] = "5"
        self.tmp = tempfile.TemporaryDirectory()
        self.p = CheckpointPolicyV91(_DisabledStore(), Path(self.tmp.name) / "s.sqlite3").persistence

    def tearDown(self):
        self.tmp.cleanup()

    def test_grouped_rows_match_per_pair_reads(self):
        rows = [_obs(f"b{i}", "C1", "BTC", 100 + i, s)
                for i, s in enumerate(("YES", "NO", "YES", "YES"))]
        rows += [_obs(f"e{i}", "C2", "ETH", 200 + i, s) for i, s in enumerate(("NO", "NO"))]
        self.p.insert_observations(rows)
        pairs = [("C1", "BTC"), ("C2", "ETH")]
        for limit in (2, 3, 10):
            batched = self.p.recent_observations_for_pairs(pairs, limit)
            for pair in pairs:
                per = self.p.recent_observations(pair[0], pair[1], limit)
                self.assertEqual(
                    [r["observation_key"] for r in batched[pair]],
                    [r["observation_key"] for r in per],
                    msg=f"pair={pair} limit={limit}",
                )

    def test_dedup_and_empty(self):
        self.assertEqual(self.p.recent_observations_for_pairs([], 5), {})
        self.p.insert_observations([_obs("x", "C", "BTC", 1, "YES")])
        dup = self.p.recent_observations_for_pairs([("C", "BTC"), ("C", "BTC")], 5)
        self.assertEqual(list(dup.keys()), [("C", "BTC")])  # one entry, not two


class RollingBatchPgSingleRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_union_all_query_for_many_pairs(self):
        store = _FakePgStore(rows=[])
        p = CheckpointPolicyV91(store, Path(self.tmp.name) / "s.sqlite3").persistence
        self.assertTrue(p.pg)
        store.queries.clear()
        p.recent_observations_for_pairs([("C1", "BTC"), ("C2", "ETH"), ("C3", "SOL")], 5)
        self.assertEqual(len(store.queries), 1)          # ONE round-trip
        sql, params = store.queries[0]
        self.assertEqual(sql.count("UNION ALL"), 2)       # 3 branches -> 2 joiners
        self.assertEqual(len(params), 9)                  # (contract, asset, limit) x 3
        self.assertEqual(params[2], 5)                    # the LIMIT param


if __name__ == "__main__":
    unittest.main()
