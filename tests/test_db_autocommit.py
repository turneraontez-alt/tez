from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class _FakeConn:
    def __init__(self):
        self.autocommit = False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.got = 0

    def getconn(self):
        self.got += 1
        return self._conn


class TestSignalStoreAutocommit(unittest.TestCase):
    """_conn() must enable autocommit so single-statement ops skip the extra
    COMMIT round-trip (the main lever on the per-asset Postgres cycle path)."""

    def _store(self, conn):
        from db import SignalStore
        store = SignalStore.__new__(SignalStore)  # bypass real pool/__init__
        store.enabled = True
        store.pool = _FakePool(conn)
        return store

    def test_conn_enables_autocommit(self):
        conn = _FakeConn()
        store = self._store(conn)
        got = store._conn()
        self.assertIs(got, conn)
        self.assertTrue(conn.autocommit)

    def test_already_autocommit_connection_is_returned_unchanged(self):
        conn = _FakeConn()
        conn.autocommit = True
        store = self._store(conn)
        got = store._conn()
        self.assertIs(got, conn)
        self.assertTrue(conn.autocommit)


if __name__ == "__main__":
    unittest.main()
