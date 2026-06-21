"""Concurrency contract for the Flask read paths.

The refresh loop publishes `state` under `state_lock` while Flask serves
`/api/*` routes from other threads (threaded=True). These tests hammer the
read routes from many threads while a writer mutates `state` the same way the
loop does, proving the lock prevents "dict changed size during iteration" and
that concurrent reads never deadlock or 500.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")  # don't spawn the live loop

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

flask = __import__("importlib").util.find_spec("flask")
if flask is None:  # pragma: no cover - environment without web deps
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


def _sample_snapshot(asset: str, score: int) -> dict:
    return {
        "asset": asset, "ticker": f"{asset}-T", "opportunity_score": score,
        "market_state": "live", "seconds_remaining": 600, "error": None,
    }


class TestConcurrentReadRoutes(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        with appmod.state_lock:
            appmod.state.clear()
            for i, a in enumerate(appmod.ASSETS):
                appmod.state[a] = _sample_snapshot(a, i)

    def test_readers_and_writer_do_not_deadlock_or_error(self):
        stop = threading.Event()
        errors: list[str] = []

        def writer():
            n = 0
            while not stop.is_set():
                # Mutate state exactly as the refresh loop does: rebuild entries
                # under the same lock the routes read with.
                with appmod.state_lock:
                    for i, a in enumerate(appmod.ASSETS):
                        appmod.state[a] = _sample_snapshot(a, (i + n) % 7)
                n += 1

        def reader(route: str):
            try:
                for _ in range(120):
                    resp = self.client.get(route)
                    if resp.status_code != 200:
                        errors.append(f"{route} -> {resp.status_code}")
                        return
                    resp.get_json()  # force full serialization of the snapshot
            except Exception as exc:  # pragma: no cover - this is the failure we guard against
                errors.append(f"{route} raised {type(exc).__name__}: {exc}")

        w = threading.Thread(target=writer, daemon=True)
        w.start()
        readers = [
            threading.Thread(target=reader, args=(r,), daemon=True)
            for r in ("/api/snapshot", "/api/snapshot", "/api/summary", "/api/snapshot")
        ]
        for t in readers:
            t.start()
        for t in readers:
            t.join(timeout=20)
            self.assertFalse(t.is_alive(), "reader thread hung (possible deadlock)")
        stop.set()
        w.join(timeout=5)
        self.assertEqual(errors, [])

    def test_snapshot_is_sorted_by_opportunity_score(self):
        with appmod.state_lock:
            appmod.state.clear()
            appmod.state["A"] = _sample_snapshot("A", 1)
            appmod.state["B"] = _sample_snapshot("B", 9)
            appmod.state["C"] = _sample_snapshot("C", 5)
        data = self.client.get("/api/snapshot").get_json()
        scores = [row["opportunity_score"] for row in data]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
