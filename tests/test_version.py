"""Tests for the deploy/version surface (version.py)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import version


class VersionTests(unittest.TestCase):
    def test_build_info_has_expected_keys(self):
        info = version.build_info()
        for key in ("commit", "branch", "committed_at", "summary", "tests"):
            self.assertIn(key, info)

    def test_payload_includes_runtime_fields(self):
        p = version.version_payload()
        for key in ("commit", "running_commit", "matches_checkout",
                    "build_info_stale", "stale_build_info_commit",
                    "process_started_at", "uptime_seconds", "server_time"):
            self.assertIn(key, p)
        self.assertGreaterEqual(p["uptime_seconds"], 0.0)

    def test_text_is_human_readable(self):
        txt = version.version_text()
        self.assertIn("build", txt)
        self.assertIn("shipped", txt)
        self.assertIn("running", txt)

    def test_missing_build_info_degrades_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.json")
            with mock.patch.object(version, "_BUILD_INFO_PATH", missing):
                info = version._read_build_info()
        self.assertEqual(info["commit"], "unknown")
        self.assertIsNone(info["summary"])

    def test_reads_a_valid_build_info_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "build_info.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"commit": "abc1234", "branch": "main",
                           "committed_at": "2026-01-01T00:00:00Z",
                           "summary": "x", "tests": "1 passed"}, fh)
            with mock.patch.object(version, "_BUILD_INFO_PATH", path):
                info = version._read_build_info()
        self.assertEqual(info["commit"], "abc1234")
        self.assertEqual(info["summary"], "x")

    def test_stale_build_info_detects_checkout_mismatch(self):
        info = {"commit": "old1234"}
        self.assertTrue(version._stale_build_info(info, "new5678"))
        self.assertFalse(version._stale_build_info(info, "old1234"))
        self.assertFalse(version._stale_build_info(info, None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
