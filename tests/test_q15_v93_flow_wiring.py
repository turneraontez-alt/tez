"""Regression tests for the live-pipeline -> v93 analyzer key wiring.

The live snapshot stores executed-flow volumes, order-book imbalance and the
underlying candle series under their own names (``taker_*_volume_15s``,
``orderbook_imbalance``, ``underlying_candles_5s``).  These tests pin the
fallbacks that let :func:`analyse_flow` and :func:`extract_completed_candles`
read those raw fields instead of reporting everything as unavailable.
"""

import unittest

from q15_upgrade.checkpoint_v93 import (
    _derived_flow_inputs,
    _underlying_momentum,
    analyse_flow,
    extract_completed_candles,
)


def _underlying_candles(base: float, step: float, count: int = 8) -> list[dict]:
    candles = []
    start = 1_781_874_000
    price = base
    for i in range(count):
        open_p = price
        close_p = price + step
        high = max(open_p, close_p) + 1.0
        low = min(open_p, close_p) - 1.0
        candles.append(
            {
                "open": open_p,
                "high": high,
                "low": low,
                "close": close_p,
                "start_time": start + i * 5,
                "end_time": start + i * 5 + 5,
            }
        )
        price += step
    return candles


class TestCandleWiring(unittest.TestCase):
    def test_extract_reads_underlying_candles_5s(self) -> None:
        snapshot = {"underlying_candles_5s": _underlying_candles(62_700.0, 2.0, 6)}
        candles, source, _ = extract_completed_candles(snapshot)
        self.assertEqual(source, "underlying_candles_5s")
        self.assertEqual(len(candles), 6)

    def test_contract_candles_are_not_used_for_wicks(self) -> None:
        # candles_5s are contract-price candles and must be ignored by the
        # wick extractor (only underlying/spot series are wired).
        snapshot = {"candles_5s": _underlying_candles(39.0, 0.5, 6)}
        candles, source, _ = extract_completed_candles(snapshot)
        self.assertEqual(candles, [])
        self.assertEqual(source, "unavailable")


class TestDerivedFlowInputs(unittest.TestCase):
    def test_derives_three_signals_from_raw_fields(self) -> None:
        snapshot = {
            "asset": "BTC",
            "taker_yes_volume_15s": 8000.0,
            "taker_no_volume_15s": 2000.0,
            "orderbook_imbalance": 0.20,
            "underlying_candles_5s": _underlying_candles(62_700.0, 3.0, 10),
        }
        derived = _derived_flow_inputs(snapshot)
        self.assertAlmostEqual(derived["executed_flow_skew"], 0.6, places=6)
        self.assertAlmostEqual(derived["order_book_imbalance"], 0.20, places=6)
        self.assertGreater(derived["spot_momentum"], 0.0)

    def test_aggressive_volume_fallback(self) -> None:
        snapshot = {
            "aggressive_yes_volume": 1000.0,
            "aggressive_no_volume": 3000.0,
        }
        derived = _derived_flow_inputs(snapshot)
        self.assertAlmostEqual(derived["executed_flow_skew"], -0.5, places=6)

    def test_momentum_sign_tracks_underlying_direction(self) -> None:
        rising = {"underlying_candles_5s": _underlying_candles(62_700.0, 4.0, 12)}
        falling = {"underlying_candles_5s": _underlying_candles(62_700.0, -4.0, 12)}
        self.assertGreater(_underlying_momentum(rising), 0.0)
        self.assertLess(_underlying_momentum(falling), 0.0)

    def test_too_few_candles_returns_none(self) -> None:
        snapshot = {"underlying_candles_5s": _underlying_candles(62_700.0, 1.0, 2)}
        self.assertIsNone(_underlying_momentum(snapshot))

    def test_falls_through_to_next_key_when_first_is_sparse(self) -> None:
        # Sparse high-res series must not shadow a usable lower-res series.
        snapshot = {
            "underlying_candles_5s": _underlying_candles(62_700.0, 4.0, 2),
            "underlying_candles_15s": _underlying_candles(62_700.0, 4.0, 10),
        }
        momentum = _underlying_momentum(snapshot)
        self.assertIsNotNone(momentum)
        self.assertGreater(momentum, 0.0)


class TestFlowBecomesAvailable(unittest.TestCase):
    def test_raw_fields_clear_insufficient_data_and_support_yes(self) -> None:
        snapshot = {
            "asset": "BTC",
            "taker_yes_volume_15s": 8000.0,
            "taker_no_volume_15s": 2000.0,
            "orderbook_imbalance": 0.30,
            "underlying_candles_5s": _underlying_candles(62_700.0, 4.0, 12),
        }
        result = analyse_flow(snapshot, "YES")
        self.assertGreaterEqual(result.available_count, 2)
        self.assertNotEqual(result.status, "INSUFFICIENT_DATA")
        self.assertEqual(result.status, "SUPPORTIVE")

    def test_same_evidence_opposes_no_side(self) -> None:
        snapshot = {
            "asset": "BTC",
            "taker_yes_volume_15s": 8000.0,
            "taker_no_volume_15s": 2000.0,
            "orderbook_imbalance": 0.30,
            "underlying_candles_5s": _underlying_candles(62_700.0, 4.0, 12),
        }
        result = analyse_flow(snapshot, "NO")
        self.assertGreaterEqual(result.available_count, 2)
        self.assertEqual(result.status, "OPPOSING")

    def test_canonical_keys_take_precedence_over_derived(self) -> None:
        snapshot = {
            "executed_flow_skew": -0.4,
            "order_book_imbalance": -0.4,
            "spot_momentum": -0.4,
            # Conflicting raw fields must be ignored when canonical keys exist.
            "taker_yes_volume_15s": 9000.0,
            "taker_no_volume_15s": 1000.0,
            "orderbook_imbalance": 0.9,
        }
        result = analyse_flow(snapshot, "YES")
        self.assertEqual(result.status, "OPPOSING")

    def test_no_flow_fields_still_insufficient(self) -> None:
        result = analyse_flow({"asset": "BTC"}, "YES")
        self.assertEqual(result.status, "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
