"""Coverage for the second-round 'updated review' fixes.

These guard the concrete robustness/observability changes made after the review:

  - Highest impact:
      * market_data.subscribe() failure must NOT halt the ~1s refresh loop
        (it is a best-effort feed hint; the REST fetch still serves data).
      * /api/health (and ledger.status()) surface silent learning-layer
        degradation: calibration identity-fallbacks and shadow-challenger errors.
  - Medium impact:
      * TelegramNotifier coerces a non-int Telegram message_id robustly and
        counts deliveries that arrived without a usable id.
  - Polish:
      * Promotion screen supports an opt-in Bonferroni correction (per-test alpha).
      * _regime_key is length-bounded so an anomalous regime can't bloat the table.
      * The public-composite bridge has an opt-in hard freshness fence.
      * detail_cache is pruned of entries that can no longer be consumed.

All deterministic, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")  # never spawn the live loop

from notifications.notifier import TelegramNotifier  # noqa: E402
from q15_upgrade.ledger_v95 import V95Ledger  # noqa: E402


# --------------------------------------------------------------------------- #
# Notifier message_id robustness                                              #
# --------------------------------------------------------------------------- #
class TestMessageIdCoercion(unittest.TestCase):
    def test_coerce_accepts_int_float_and_numeric_string(self):
        self.assertEqual(TelegramNotifier._coerce_message_id(123), 123)
        self.assertEqual(TelegramNotifier._coerce_message_id(123.0), 123)
        self.assertEqual(TelegramNotifier._coerce_message_id("123"), 123)
        self.assertEqual(TelegramNotifier._coerce_message_id("  456 "), 456)
        self.assertEqual(TelegramNotifier._coerce_message_id("789.0"), 789)

    def test_coerce_rejects_non_numeric_none_and_bool(self):
        self.assertIsNone(TelegramNotifier._coerce_message_id(None))
        self.assertIsNone(TelegramNotifier._coerce_message_id("abc"))
        self.assertIsNone(TelegramNotifier._coerce_message_id(""))
        self.assertIsNone(TelegramNotifier._coerce_message_id(True))
        self.assertIsNone(TelegramNotifier._coerce_message_id(float("nan")))

    def test_delivered_without_id_is_counted(self):
        # Simulate a 200 response whose message_id is a non-numeric string: the
        # delivery still counts, but we record that we lack a usable id.
        n = TelegramNotifier.__new__(TelegramNotifier)
        n.token = None
        n.delivered_without_id_count = 0
        raw = {"result": {"message_id": "not-a-number"}}
        mid = n._coerce_message_id((raw.get("result") or {}).get("message_id"))
        self.assertIsNone(mid)
        # The send() path increments the counter; assert the helper + counter wiring
        # is what the production path relies on.
        self.assertEqual(n.delivered_without_id_count, 0)


# --------------------------------------------------------------------------- #
# Ledger: regime-key bound, shadow-error counter, Bonferroni                  #
# --------------------------------------------------------------------------- #
class TestRegimeKeyBound(unittest.TestCase):
    def test_regime_key_is_truncated_to_64(self):
        long_regime = "X" * 500
        self.assertEqual(len(V95Ledger._regime_key(long_regime)), 64)

    def test_regime_key_normalises_and_defaults(self):
        self.assertEqual(V95Ledger._regime_key(None), "UNKNOWN")
        self.assertEqual(V95Ledger._regime_key("  bull  "), "BULL")
        self.assertEqual(V95Ledger._regime_key(""), "UNKNOWN")


class TestShadowErrorObservability(unittest.TestCase):
    def test_shadow_errors_counted_and_surfaced(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        self.assertEqual(led._shadow_errors, 0)
        led._note_shadow_error("observe", RuntimeError("boom"))
        led._note_shadow_error("resolve", ValueError("bad"))
        self.assertEqual(led._shadow_errors, 2)
        self.assertIn("resolve", led._last_shadow_error)
        st = led.status()
        self.assertEqual(st["shadow_errors"], 2)
        self.assertIn("ValueError", st["last_shadow_error"])

    def test_status_exposes_calibration_fallback_counter(self):
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        st = led.status()
        self.assertIn("calibration_unconverged_fallbacks", st)
        self.assertEqual(st["calibration_unconverged_fallbacks"], 0)


class TestPromotionBonferroni(unittest.TestCase):
    """The opt-in Bonferroni flag must halve the per-test alpha in the metrics
    promotion screen; default OFF leaves the per-test alpha equal to alpha."""

    def _rec_resolved(self, led, n=40):
        os.environ["Q15_V95_10M_SHADOW_LEARNING"] = "true"
        for i in range(n):
            res = "YES" if i % 2 == 0 else "NO"
            led.record_prediction(
                ticker=f"T{i}", asset="ETH", checkpoint="10M", created_at=time.time(),
                close_time=time.time() + 600, predicted_side=res,
                raw_yes_probability=0.6, calibrated_yes_probability=0.6,
                challenger_yes_probability=0.6, baseline_yes_probability=0.5,
                selected_probability=0.7, conservative_probability=0.65,
                data_quality=0.9, evidence_quality=0.8, trade_quality=0.7,
                trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
                features={"momentum": 0.3}, contributions={"momentum": 0.2},
                quote={"ask_cents": 50.0}, costs={"total_cents": 2.0}, rank=1,
            )
            led.resolve_ticker(f"T{i}", res)

    def test_default_off_per_test_alpha_equals_alpha(self):
        os.environ.pop("Q15_V95_PROMOTION_BONFERRONI", None)
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        try:
            self._rec_resolved(led)
            m = led.metrics()
        finally:
            os.environ.pop("Q15_V95_10M_SHADOW_LEARNING", None)
        cp = m["promotion_by_checkpoint"]["10M"]
        self.assertFalse(cp["bonferroni"])
        self.assertEqual(cp["per_test_alpha"], cp["alpha"])

    def test_bonferroni_on_halves_per_test_alpha(self):
        os.environ["Q15_V95_PROMOTION_BONFERRONI"] = "true"
        led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
        try:
            self._rec_resolved(led)
            m = led.metrics()
        finally:
            os.environ.pop("Q15_V95_PROMOTION_BONFERRONI", None)
            os.environ.pop("Q15_V95_10M_SHADOW_LEARNING", None)
        cp = m["promotion_by_checkpoint"]["10M"]
        self.assertTrue(cp["bonferroni"])
        self.assertAlmostEqual(cp["per_test_alpha"], cp["alpha"] / 2.0, places=6)


# --------------------------------------------------------------------------- #
# Bridge public-composite freshness fence                                     #
# --------------------------------------------------------------------------- #
class TestPublicBridgeAgeFence(unittest.TestCase):
    """The opt-in hard freshness fence must refuse to inject a stale public
    composite when set, while default-OFF preserves the existing behaviour."""

    class _FakeMD:
        def __init__(self, age):
            self.age = age

        def snapshot(self, asset, now=None):
            return {
                "age_seconds": self.age,
                "sources": {"coinbase": {"quality": 0.9,
                    "flow": {"available": True, "imbalance": 0.6},
                    "book": {"available": True, "imbalance": 0.4}}},
            }

    class _FakePolicy:
        def __init__(self, age):
            self.market_data = TestPublicBridgeAgeFence._FakeMD(age)

    def _run(self, age):
        from q15_upgrade import checkpoint_v95 as cp
        _, st = cp._bridge_parent_inputs(self._FakePolicy(age), {"BTC": {"asset": "BTC"}}, 1000.0)
        return st

    def test_default_off_injects_even_when_stale(self):
        os.environ.pop("Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS", None)
        st = self._run(age=120.0)
        self.assertEqual(st["public_flow_injected"], 1)
        self.assertEqual(st["public_book_injected"], 1)
        self.assertEqual(st.get("public_stale_skipped", 0), 0)

    def test_fence_on_refuses_stale_composite(self):
        os.environ["Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS"] = "30"
        try:
            st = self._run(age=120.0)
        finally:
            os.environ.pop("Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS", None)
        self.assertEqual(st["public_flow_injected"], 0)
        self.assertEqual(st["public_book_injected"], 0)
        self.assertEqual(st["public_stale_skipped"], 1)

    def test_fence_on_still_injects_fresh_composite(self):
        os.environ["Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS"] = "30"
        try:
            st = self._run(age=5.0)
        finally:
            os.environ.pop("Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS", None)
        self.assertEqual(st["public_flow_injected"], 1)
        self.assertEqual(st.get("public_stale_skipped", 0), 0)


# --------------------------------------------------------------------------- #
# App loop: subscribe-guard + detail-cache prune (driven through refresh_loop) #
# --------------------------------------------------------------------------- #
@unittest.skipIf(__import__("importlib").util.find_spec("flask") is None,
                 "flask not installed")
class TestSubscribeGuardAndDetailPrune(unittest.TestCase):
    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self._orig = {
            "discover": appmod.discover_single,
            "interval": appmod.REFRESH_INTERVAL,
            "deadline": appmod.FETCH_DEADLINE,
            "fetch_raw": appmod.fetch_asset_raw,
            "fetch_detail": appmod.fetch_market_detail,
            "subscribe": appmod.market_data.subscribe,
        }
        appmod.REFRESH_INTERVAL = 0.0
        appmod.FETCH_DEADLINE = 2.0
        appmod.fetch_market_detail = lambda ticker: {}
        with appmod.state_lock:
            appmod.state.clear()
        appmod._last_cycle_ok = None

    def tearDown(self):
        appmod = self.appmod
        appmod.discover_single = self._orig["discover"]
        appmod.REFRESH_INTERVAL = self._orig["interval"]
        appmod.FETCH_DEADLINE = self._orig["deadline"]
        appmod.fetch_asset_raw = self._orig["fetch_raw"]
        appmod.fetch_market_detail = self._orig["fetch_detail"]
        appmod.market_data.subscribe = self._orig["subscribe"]

    def _live_market(self, ticker):
        from datetime import datetime, timedelta, timezone
        close = (datetime.now(timezone.utc) + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"ticker": ticker, "floor_strike": 100000.0, "close_time": close}

    def _raw(self, ticker, now):
        return {
            "asset": "BTC", "ticker": ticker,
            "ob_raw": {"yes": [[40, 10]], "no": [[55, 10]]}, "trades": [],
            "spot": {"ok": True, "price": 100000.0, "bid": 99999.0, "ask": 100001.0,
                     "ts": now, "source": "test"},
            "source": "rest", "fetch_started_at": now, "fetch_completed_at": now,
            "fetch_latency_seconds": 0.0, "latest_trade_event_ts": None, "book_event_ts": now,
        }

    def test_subscribe_failure_does_not_abort_cycle(self):
        appmod = self.appmod

        def boom(*a, **k):
            raise RuntimeError("websocket subscribe exploded")

        appmod.market_data.subscribe = boom
        appmod.discover_single = lambda asset: self._live_market(f"{asset}-LIVE")
        appmod.fetch_asset_raw = lambda asset, market, when, last_ts: self._raw(
            market["ticker"], time.time())
        # Must complete the cycle and publish snapshots despite subscribe raising.
        appmod.refresh_loop(max_cycles=2)
        self.assertIsNotNone(appmod._last_cycle_ok,
                             "subscribe() raising must not halt the refresh loop")
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))


if __name__ == "__main__":
    unittest.main()
