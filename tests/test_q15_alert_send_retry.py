from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager


class _Store:
    """Minimal store with no claim_event -> manager uses local claims only."""

    enabled = False


class _RaisingStore:
    enabled = True

    def __init__(self):
        self.calls = 0

    def claim_event(self, key):
        self.calls += 1
        raise RuntimeError("store unavailable")


class _Notifier:
    enabled = True

    def __init__(self, results=None):
        self.messages = []
        # ``results`` is a list of bool return values consumed per send; once
        # exhausted, sends succeed. ``None`` entry -> raise instead of return.
        self._results = list(results or [])

    def send(self, msg):
        self.messages.append(msg)
        if self._results:
            outcome = self._results.pop(0)
            if outcome is None:
                raise RuntimeError("telegram boom")
            return outcome
        return True


def _snap(edge, side="YES", win=0.62, seconds=540, ticker="BTC-T"):
    return {
        "ticker": ticker,
        "seconds_remaining": seconds,
        "calibrated_edge_side": side,
        "conservative_edge_after_costs_cents": edge,
        "calibrated_side_probability": win,
        "yes_ask": 41.0,
        "no_ask": 41.0,
    }


def _mgr(notifier, settings=None, store=None):
    return TwoWindowFocusManager(
        store if store is not None else _Store(),
        notifier,
        None,
        None,
        settings or FocusSettings(),
    )


class ClaimFailClosedTest(unittest.TestCase):
    def test_claim_returns_false_when_store_raises(self):
        # Fix #2: a store outage must NOT let us claim locally and send, or
        # dev+prod would each deliver the same alert.
        mgr = _mgr(_Notifier(), store=_RaisingStore())
        self.assertFalse(mgr._claim("k1"))
        # Local claim set stays empty so a recovered store can claim later.
        self.assertNotIn("k1", mgr._local_claims)


class ClaimAndSendTest(unittest.TestCase):
    def test_sent_on_success(self):
        n = _Notifier()
        mgr = _mgr(n)
        self.assertEqual(mgr._claim_and_send("k", "hi"), "sent")
        self.assertEqual(n.messages, ["hi"])

    def test_duplicate_when_already_claimed(self):
        n = _Notifier()
        mgr = _mgr(n)
        mgr._local_claims.add("k")
        self.assertEqual(mgr._claim_and_send("k", "hi"), "duplicate")
        self.assertEqual(n.messages, [])

    def test_failed_releases_claim_for_retry(self):
        # Fix #1: a delivery failure releases the local claim and reports
        # "failed" so the caller can retry instead of silently dropping.
        n = _Notifier(results=[False])
        mgr = _mgr(n)
        self.assertEqual(mgr._claim_and_send("k", "hi"), "failed")
        self.assertNotIn("k", mgr._local_claims)
        # Next attempt re-claims and succeeds.
        self.assertEqual(mgr._claim_and_send("k", "hi"), "sent")
        self.assertEqual(n.messages, ["hi", "hi"])

    def test_send_exception_is_treated_as_failure(self):
        n = _Notifier(results=[None])  # raise on first send
        mgr = _mgr(n)
        self.assertEqual(mgr._claim_and_send("k", "hi"), "failed")
        self.assertNotIn("k", mgr._local_claims)

    def test_legacy_consumes_claim_when_retry_disabled(self):
        n = _Notifier(results=[False])
        mgr = _mgr(n, settings=FocusSettings(alert_retry_on_send_failure=False))
        # Reports "sent" (legacy) and the claim is consumed -> no retry.
        self.assertEqual(mgr._claim_and_send("k", "hi"), "sent")
        self.assertIn("k", mgr._local_claims)
        self.assertEqual(mgr._claim_and_send("k", "hi"), "duplicate")


class DipAlertRetryTest(unittest.TestCase):
    def test_dip_alert_retries_after_send_failure(self):
        # First send fails -> trigger stays armed, counters untouched ->
        # the same dip event re-fires next cycle on the SAME claim key.
        n = _Notifier(results=[False])
        mgr = _mgr(n)
        mgr._maybe_dip_alert("k", {"BTC": _snap(edge=7.5)}, 0.0)
        self.assertEqual(len(n.messages), 1)  # attempted, failed
        # Re-arm not needed: state was not advanced. Retry next cycle.
        mgr._maybe_dip_alert("k", {"BTC": _snap(edge=7.5)}, 1.0)
        self.assertEqual(len(n.messages), 2)  # retried, succeeded

    def test_dip_alert_does_not_retry_when_disabled(self):
        n = _Notifier(results=[False])
        mgr = _mgr(n, settings=FocusSettings(alert_retry_on_send_failure=False))
        mgr._maybe_dip_alert("k", {"BTC": _snap(edge=7.5)}, 0.0)
        self.assertEqual(len(n.messages), 1)
        # State advanced (legacy) -> armed False, no resend for the same event.
        mgr._maybe_dip_alert("k", {"BTC": _snap(edge=7.5)}, 1.0)
        self.assertEqual(len(n.messages), 1)


if __name__ == "__main__":
    unittest.main()
