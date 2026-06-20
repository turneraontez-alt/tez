"""Proves the default-OFF fast candle path is byte-identical to the frozen one.

``fast_canonical_candles`` must return EXACTLY what the frozen
``_canonical_candles`` returns for every input, since real money trades off the
predictions built on these candles.  We assert equality across:

* the real cached-row shape (no ``timestamp`` field; lacks clamped hi/lo),
* live candle shapes (short aliases o/h/l/c, ``last``, ms + ISO timestamps),
* missing optional/time fields (volume, trade_count, end_time, start_time),
* rows whose extra keys partial-match an alias (e.g. ``interval`` contains "v"),
* invalid rows (non-positive or non-numeric OHLC),
* candle lists nested under arbitrary keys, tuples vs lists, dedup overlaps,
* histories longer than the 600-row tail, and a randomized fuzz.

Plus: the v9.5 dispatcher honours ``Q15_FAST_CANONICAL_CANDLES`` and
``build_canonical_snapshot`` produces an identical snapshot either way.
"""
from __future__ import annotations

import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v94_unified import _canonical_candles
from q15_upgrade.fast_candles import fast_canonical_candles


def _assert_same(test, snapshot, cached):
    expected = _canonical_candles(snapshot, cached)
    actual = fast_canonical_candles(snapshot, cached)
    test.assertEqual(actual, expected)
    # Also assert exact float identity of every field (==, not approx).
    test.assertEqual(len(actual), len(expected))
    for a, e in zip(actual, expected):
        test.assertEqual(sorted(a.keys()), sorted(e.keys()))
        for k in e:
            test.assertEqual(a[k], e[k], msg=f"field {k}: {a[k]!r} != {e[k]!r}")


class TestFastCanonicalCandlesEquivalence(unittest.TestCase):
    def test_cached_rows_real_shape(self):
        # Exactly what checkpoint_v94._load_persistent_cache produces: 8 float
        # keys, no `timestamp`. This is the bulk of the history.
        cached = [
            {
                "start_time": 1_700_000_000.0 + i * 5,
                "end_time": 1_700_000_005.0 + i * 5,
                "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
                "close": 100.5 + i, "volume": float(i), "trade_count": float(i % 3),
            }
            for i in range(50)
        ]
        _assert_same(self, {}, cached)

    def test_short_aliases_and_last(self):
        rows = [
            {"o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "ts": 1_700_000_000.0},
            {"open": 10.0, "high": 11.0, "low": 9.0, "last": 10.7, "start": 1_700_000_005.0},
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_missing_optional_and_time_fields(self):
        rows = [
            # no volume / trade_count -> default 0.0 via the frozen walk fallback
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "start_time": 1_700_000_000.0},
            # no end_time -> derived start+5 ; no start -> derived from timestamp
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "timestamp": 1_700_000_050.0},
        ]
        _assert_same(self, {"underlying_candles_5s": rows}, None)

    def test_partial_match_extra_keys(self):
        # `interval` normalises to contain "v" (volume alias) -> frozen path walks
        # and may pick it up. `approval_count` contains "count" (trade alias).
        rows = [
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
             "start_time": 1_700_000_000.0, "interval": 5.0, "approval_count": 7.0},
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_string_iso_and_ms_timestamps(self):
        rows = [
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
             "start_time": "2026-06-20T11:00:00Z", "end_time": "2026-06-20T11:00:05Z"},
            {"open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5,
             "start_time": 1_700_000_010_000.0, "end_time": 1_700_000_015_000.0},  # ms
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_invalid_rows_rejected_identically(self):
        rows = [
            {"open": 0.0, "high": 1.0, "low": 0.0, "close": 0.5, "start_time": 1_700_000_000.0},  # <=0
            {"open": "abc", "high": 1.0, "low": 0.5, "close": 0.7, "start_time": 1_700_000_005.0},  # NaN
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "start_time": 1_700_000_010.0},  # ok
            "not-a-mapping",
            42,
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_nested_candle_row_uses_frozen_path(self):
        # A row with a nested container value -> _fast_normalize_candle must defer
        # to the frozen function (which would walk into the nested structure).
        rows = [
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
             "start_time": 1_700_000_000.0, "meta": {"nested_volume": 5.0}},
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_nested_candle_lists_anywhere(self):
        snapshot = {
            "foo": {"bar_candles": [
                {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "start_time": 1_700_000_000.0},
            ]},
            "deep": {"deeper": {"ohlc_history": [
                {"open": 2.0, "high": 3.0, "low": 1.5, "close": 2.5, "start_time": 1_700_000_005.0},
            ]}},
        }
        _assert_same(self, snapshot, None)

    def test_tuple_containers(self):
        rows = (
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "start_time": 1_700_000_000.0},
            {"open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5, "start_time": 1_700_000_005.0},
        )
        _assert_same(self, {"candles": rows}, None)

    def test_dedup_overlap_across_candidates(self):
        # Same start_time across cached + snapshot: later candidate wins. Ordering
        # of candidate processing must match the frozen function.
        cached = [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                   "start_time": 1_700_000_000.0, "end_time": 1_700_000_005.0,
                   "volume": 1.0, "trade_count": 1.0}]
        snap_rows = [{"open": 9.0, "high": 9.5, "low": 8.5, "close": 9.2,
                      "start_time": 1_700_000_000.0}]
        _assert_same(self, {"candles": snap_rows}, cached)

    def test_history_longer_than_tail(self):
        cached = [
            {"start_time": 1_700_000_000.0 + i, "end_time": 1_700_000_001.0 + i,
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
             "volume": float(i), "trade_count": 0.0}
            for i in range(900)  # > 600 tail and > 1000? no, exercises [-600:]
        ]
        _assert_same(self, {}, cached)

    def test_zero_start_time_dedup_uses_timestamp(self):
        rows = [
            {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
             "start_time": 0.0, "end_time": 1_700_000_005.0},
        ]
        _assert_same(self, {"candles": rows}, None)

    def test_empty_inputs(self):
        _assert_same(self, {}, None)
        _assert_same(self, {}, [])
        _assert_same(self, {"candles": []}, None)

    def test_randomized_fuzz(self):
        rng = random.Random(20260620)
        key_pool_ohlc = [("open", "o"), ("high", "h"), ("low", "l"), ("close", "c", "last")]
        time_keys = ["start_time", "start", "timestamp", "time", "ts", "bucket",
                     "end_time", "end", "close_time"]
        noise_keys = ["interval", "approval_count", "vwap", "avg", "count_total", "trades_extra"]

        def rand_value():
            r = rng.random()
            if r < 0.7:
                return round(rng.uniform(0.01, 5000.0), 4)
            if r < 0.8:
                return 0.0  # triggers <=0 rejection sometimes
            if r < 0.9:
                return "not-a-number"
            return round(rng.uniform(-10.0, 10.0), 4)

        def rand_ts():
            r = rng.random()
            base = 1_700_000_000.0 + rng.randint(0, 10000)
            if r < 0.5:
                return base
            if r < 0.7:
                return base * 1000.0  # ms
            if r < 0.85:
                return "2026-06-20T11:00:00Z"
            return None

        def rand_row():
            row = {}
            for aliases in key_pool_ohlc:
                if rng.random() < 0.9:
                    row[rng.choice(aliases)] = rand_value()
            for tk in time_keys:
                if rng.random() < 0.3:
                    v = rand_ts()
                    if v is not None:
                        row[tk] = v
            if rng.random() < 0.4:
                row[rng.choice(("volume", "v"))] = round(rng.uniform(0, 100), 3)
            if rng.random() < 0.4:
                row[rng.choice(("trade_count", "trades", "count"))] = float(rng.randint(0, 50))
            for nk in noise_keys:
                if rng.random() < 0.25:
                    row[nk] = round(rng.uniform(0, 10), 3)
            if rng.random() < 0.1:
                row["meta"] = {"x": [1, 2, 3]}  # nested -> frozen fallback
            return row

        for _ in range(400):
            n = rng.randint(0, 12)
            rows = [rand_row() for _ in range(n)]
            if rng.random() < 0.3:
                rows.append(rng.choice(["junk", 7, None]))
            cached = None
            if rng.random() < 0.5:
                cached = [
                    {"start_time": 1_700_000_000.0 + i * 5,
                     "end_time": 1_700_000_005.0 + i * 5,
                     "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
                     "close": 100.5 + i, "volume": float(i), "trade_count": float(i % 4)}
                    for i in range(rng.randint(0, 8))
                ]
            key = rng.choice(["candles", "ohlc", "bars", "spot_candles_5s",
                              "underlying_candles", "weird_candle_blob"])
            snapshot = {key: rows, "unrelated": {"noise": rng.random()}}
            _assert_same(self, snapshot, cached)


class TestFastCandleDispatch(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("Q15_FAST_CANONICAL_CANDLES")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("Q15_FAST_CANONICAL_CANDLES", None)
        else:
            os.environ["Q15_FAST_CANONICAL_CANDLES"] = self._prev

    def _sample(self):
        snapshot = {
            "candles": [
                {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                 "start_time": 1_700_000_000.0, "end_time": 1_700_000_005.0,
                 "volume": 1.0, "trade_count": 2.0}
            ],
            "seconds_remaining": 420, "target": 100.0, "underlying_current": 100.4,
            "yes_bid": 40, "yes_ask": 42, "ticker": "KXBTC-X",
        }
        return snapshot

    def test_dispatch_off_uses_frozen_on_uses_fast(self):
        from q15_upgrade.checkpoint_v95 import _build_candles
        snap = self._sample()
        cached = None
        os.environ.pop("Q15_FAST_CANONICAL_CANDLES", None)
        off = _build_candles(snap, cached)
        os.environ["Q15_FAST_CANONICAL_CANDLES"] = "true"
        on = _build_candles(snap, cached)
        self.assertEqual(on, off)
        self.assertEqual(on, _canonical_candles(snap, cached))

    def test_build_canonical_snapshot_identical_either_way(self):
        from q15_upgrade.checkpoint_v95 import build_canonical_snapshot
        snap = self._sample()
        kwargs = dict(asset="BTC", checkpoint="15M", now=1_700_000_020.0,
                      cached_candles=None, context=None, public=None)
        os.environ.pop("Q15_FAST_CANONICAL_CANDLES", None)
        off = build_canonical_snapshot(snap, **kwargs)
        os.environ["Q15_FAST_CANONICAL_CANDLES"] = "1"
        on = build_canonical_snapshot(snap, **kwargs)
        self.assertEqual(on.candles, off.candles)
        self.assertEqual(on.spot, off.spot)
        self.assertEqual(on.data_quality, off.data_quality)
        self.assertEqual(on.core_errors, off.core_errors)


if __name__ == "__main__":
    unittest.main()
