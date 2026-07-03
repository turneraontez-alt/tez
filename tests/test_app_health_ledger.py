"""/api/health surfaces learning-ledger health at the top level.

The review flagged that when the ledger is unavailable, calibration silently
falls back to identity while health only showed it buried in q15_v9_5.ledger.
This asserts a compact top-level `ledger` block (with an `available` flag) so an
operator can see "ledger down" at a glance, and that a ledger error never breaks
the health route itself.
"""
from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class TestHealthLedgerSurface(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    def test_health_includes_top_level_ledger_block(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("ledger", body)
        self.assertIn("available", body["ledger"])
        self.assertIsInstance(body["ledger"]["available"], bool)

    def test_unavailable_ledger_is_visible_at_top_level(self):
        # When the ledger is down it returns an unavailable status (calibration
        # then falls back to identity); the top-level block must reflect that so
        # the degradation is not hidden in q15_v9_5.ledger.
        orig = appmod.checkpoint_v95.ledger.status
        appmod.checkpoint_v95.ledger.status = lambda: {
            "available": False, "path": "/data/x.db", "error": "ledger offline",
        }
        try:
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertFalse(body["ledger"]["available"])
            self.assertEqual(body["ledger"]["error"], "ledger offline")
        finally:
            appmod.checkpoint_v95.ledger.status = orig

    def test_health_includes_grading_backlog_block(self):
        orig = appmod.checkpoint_v95.ledger.reconcile_backlog_status

        def fake_grading(*, now=None):
            return {
                "available": True,
                "resolved_24h": 7,
                "unresolved_pastclose": 42,
                "oldest_unresolved_age_seconds": 900,
                "newest_resolved_at": 123.0,
                "newest_resolved_age_seconds": 60,
                "parked": 3,
            }

        appmod.checkpoint_v95.ledger.reconcile_backlog_status = fake_grading
        try:
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["grading"]["resolved_24h"], 7)
            self.assertEqual(body["grading"]["unresolved_pastclose"], 42)
            self.assertEqual(body["grading"]["parked"], 3)
            self.assertEqual(body["q15_v9_5"]["grading"]["resolved_24h"], 7)
        finally:
            appmod.checkpoint_v95.ledger.reconcile_backlog_status = orig

    def test_health_includes_coinbase_adv_l2_snapshot_age_alias(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("coinbase_adv_l2", body)
        self.assertIn("coinbase_adv_l2_snapshot_age_seconds", body)

    def test_health_includes_runtime_manifest_and_strangle_shadow(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("startup_config_manifest", body)
        self.assertIn("ok", body["startup_config_manifest"])
        self.assertIn("strangle_shadow", body)
        self.assertIn("latest_age_seconds", body["strangle_shadow"])

    def test_enabled_empty_collector_gets_watchdog_age_from_startup(self):
        orig_started = appmod.SERVER_STARTED_AT
        appmod.SERVER_STARTED_AT = 100.0
        try:
            self.assertEqual(
                appmod._feed_watchdog_age({"enabled": True, "status": "empty"}, "age", 1000.0),
                900.0,
            )
            self.assertIsNone(
                appmod._feed_watchdog_age({"enabled": False, "status": "disabled"}, "age", 1000.0)
            )
        finally:
            appmod.SERVER_STARTED_AT = orig_started


if __name__ == "__main__":
    unittest.main()
