from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.market_data_v95 import ASSET_ROUTES, PublicMarketDataHub

NOW = 1_900_000_000.0


def _okx_ticker(price):
    return {"code": "0", "data": [{"instId": "X", "last": str(price), "ts": str(int(NOW * 1000))}]}


def _okx_books():
    return {"code": "0", "data": [{
        "asks": [["101.0", "5", "0", "1"], ["101.5", "3", "0", "1"]],
        "bids": [["100.0", "6", "0", "1"], ["99.5", "4", "0", "1"]],
        "ts": str(int(NOW * 1000)),
    }]}


def _okx_trades():
    return {"code": "0", "data": [
        {"instId": "X", "px": "100.5", "sz": "2", "side": "buy", "ts": str(int(NOW * 1000))},
        {"instId": "X", "px": "100.4", "sz": "1", "side": "sell", "ts": str(int(NOW * 1000))},
    ]}


class FakeClient:
    """Returns OKX-shaped payloads by endpoint; records calls."""
    def __init__(self, price=100.5, fail=False):
        self.price = price
        self.fail = fail
        self.calls = []

    def get(self, base_url, params=None):
        self.calls.append((base_url, dict(params or {})))
        if self.fail:
            raise RuntimeError("network:down")
        if base_url.endswith("/ticker"):
            return _okx_ticker(self.price)
        if base_url.endswith("/books"):
            return _okx_books()
        if base_url.endswith("/trades"):
            return _okx_trades()
        raise AssertionError(f"unexpected url {base_url}")


class TestOkxRoutes(unittest.TestCase):
    def test_bnb_and_hype_route_to_okx_only(self):
        for asset in ("BNB", "HYPE"):
            route = ASSET_ROUTES[asset]
            self.assertIsNone(route.coinbase_product, f"{asset} should not use coinbase")
            self.assertIsNone(route.kraken_pair, f"{asset} should not use kraken")
            self.assertTrue(route.okx_instrument, f"{asset} should have an OKX instrument")

    def test_fetch_okx_parses_price_book_and_flow(self):
        hub = PublicMarketDataHub()
        hub._client = FakeClient(price=612.34)
        out = hub._fetch_okx("BNB-USDT", NOW)
        self.assertEqual(out["source"], "okx")
        self.assertAlmostEqual(out["price"], 612.34)
        self.assertTrue(out["book"]["available"])
        self.assertTrue(out["flow"]["available"])
        # taker buy 2 @100.5 vs sell 1 @100.4 -> net buy imbalance > 0
        self.assertGreater(out["flow"]["imbalance"], 0.0)

    def test_fetch_asset_bnb_yields_composite_without_errors(self):
        hub = PublicMarketDataHub()
        hub._client = FakeClient(price=550.0)
        # Disable the exchanges that don't list BNB so the test is hermetic.
        hub.coinbase_enabled = hub.kraken_enabled = hub.deribit_enabled = False
        result = hub._fetch_asset("BNB", NOW)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["composite_price"], 550.0)
        self.assertEqual(result["source_count"], 1)
        self.assertIn("okx", result["sources"])
        self.assertFalse(result["source_errors"])

    def test_okx_error_is_isolated_not_fatal(self):
        hub = PublicMarketDataHub()
        hub._client = FakeClient(fail=True)
        hub.coinbase_enabled = hub.kraken_enabled = hub.deribit_enabled = False
        result = hub._fetch_asset("HYPE", NOW)
        self.assertFalse(result["available"])
        self.assertIn("okx", result["source_errors"])


if __name__ == "__main__":
    unittest.main()


class TestParseTs(unittest.TestCase):
    def test_normalizes_millisecond_epochs_from_strings_and_numbers(self):
        from q15_upgrade.market_data_v95 import _parse_ts
        seconds = 1_900_000_000.0
        # numeric ms, string ms, and string seconds all collapse to seconds
        self.assertAlmostEqual(_parse_ts(int(seconds * 1000)), seconds)
        self.assertAlmostEqual(_parse_ts(str(int(seconds * 1000))), seconds)
        self.assertAlmostEqual(_parse_ts(str(int(seconds))), seconds)
        # microsecond epoch also collapses
        self.assertAlmostEqual(_parse_ts(str(int(seconds * 1_000_000))), seconds)

    def test_iso_string_still_parses(self):
        from q15_upgrade.market_data_v95 import _parse_ts
        self.assertIsNotNone(_parse_ts("2024-01-01T00:00:00Z"))

    def test_garbage_is_none(self):
        from q15_upgrade.market_data_v95 import _parse_ts
        self.assertIsNone(_parse_ts("not-a-time"))
        self.assertIsNone(_parse_ts(None))
