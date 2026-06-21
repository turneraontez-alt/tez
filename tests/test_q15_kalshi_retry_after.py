from __future__ import annotations

import os
import sys
import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import kalshi_rest


class _Resp:
    def __init__(self, headers):
        self.headers = headers


class RetryAfterTest(unittest.TestCase):
    def test_numeric_header_honored(self):
        r = _Resp({"Retry-After": "3"})
        self.assertEqual(kalshi_rest._retry_after_seconds(r, fallback=0.5), 3.0)

    def test_missing_header_uses_fallback(self):
        r = _Resp({})
        self.assertEqual(kalshi_rest._retry_after_seconds(r, fallback=0.5), 0.5)

    def test_capped_at_max(self):
        r = _Resp({"Retry-After": "999"})
        self.assertEqual(
            kalshi_rest._retry_after_seconds(r, fallback=0.5),
            kalshi_rest._MAX_RETRY_AFTER_SECONDS,
        )

    def test_unparseable_header_uses_fallback(self):
        r = _Resp({"Retry-After": "soon"})
        self.assertEqual(kalshi_rest._retry_after_seconds(r, fallback=0.5), 0.5)

    def test_http_date_header_honored(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=4)
        r = _Resp({"Retry-After": format_datetime(when)})
        wait = kalshi_rest._retry_after_seconds(r, fallback=0.5)
        # ~4s (allowing for clock/parsing drift), capped below the max.
        self.assertGreater(wait, 1.0)
        self.assertLessEqual(wait, kalshi_rest._MAX_RETRY_AFTER_SECONDS)

    def test_past_http_date_clamps_to_zero(self):
        when = datetime.now(timezone.utc) - timedelta(seconds=30)
        r = _Resp({"Retry-After": format_datetime(when)})
        self.assertEqual(kalshi_rest._retry_after_seconds(r, fallback=0.5), 0.0)


if __name__ == "__main__":
    unittest.main()
