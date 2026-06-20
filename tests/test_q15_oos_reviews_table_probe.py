"""Locks the OOS reviews-table probe: never SELECT a missing relation.

Querying q15_ten_minute_reviews / q15_10m_reviews when they don't exist made
SignalStore.query log "relation does not exist" and roll back the txn every
cycle. _postgres_rows now probes with to_regclass() first and only SELECTs tables
that exist.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.oos_v9 import OutOfSampleEvaluator


class _FakeStore:
    def __init__(self, existing_tables):
        self.enabled = True
        self.existing = set(existing_tables)
        self.calls: list[str] = []

    def query(self, sql, params=None):
        self.calls.append(sql)
        if "to_regclass" in sql:
            table = (params or (None,))[0]
            return [{"reg": table if table in self.existing else None}]
        # A real SELECT: return one resolved row so we can detect it ran.
        return [{"status": "RESOLVED", "actual_side": "YES", "predicted_at": 1.0,
                 "predicted_side": "YES", "predicted_probability": 0.6}]


class TestOosReviewsTableProbe(unittest.TestCase):
    def test_missing_tables_are_never_selected(self):
        store = _FakeStore(existing_tables=())  # neither table exists
        rows = OutOfSampleEvaluator(store=store)._postgres_rows()
        self.assertEqual(rows, [])
        # Only to_regclass probes — never a SELECT * FROM the missing relations.
        self.assertTrue(all("to_regclass" in c for c in store.calls), store.calls)
        self.assertFalse(any("SELECT * FROM" in c for c in store.calls), store.calls)

    def test_present_table_is_queried(self):
        store = _FakeStore(existing_tables=("q15_ten_minute_reviews",))
        rows = OutOfSampleEvaluator(store=store)._postgres_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("SELECT * FROM q15_ten_minute_reviews" in c for c in store.calls), store.calls)

    def test_disabled_store_does_nothing(self):
        store = _FakeStore(existing_tables=("q15_ten_minute_reviews",))
        store.enabled = False
        self.assertEqual(OutOfSampleEvaluator(store=store)._postgres_rows(), [])
        self.assertEqual(store.calls, [])


if __name__ == "__main__":
    unittest.main()
