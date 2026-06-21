from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.manipulation_alert import (
    ManipCandidate, NormalCheck, qualifies, build_combined_alert, ALERT_MARKER,
)


def _cand(**kw):
    base = dict(asset="BTC", checkpoint="10M", ticker="KXBTC-10M-1",
               probability=0.80, confidence=65.0, evidence=["order-book imbalance"],
               original_side="YES", original_action="ENTRY_RECOMMENDED",
               new_side="NO", new_action="stand down — do not enter (manipulation flip risk)")
    base.update(kw)
    return ManipCandidate(**base)


class GateConditionTest(unittest.TestCase):
    def test_blocks_when_normal_check_not_delivered(self):
        # Condition 2: no manipulation alert before the normal check is delivered.
        cand = _cand()
        muted = NormalCheck(checkpoint="10M", delivered=False, side="YES")
        ok, reason = qualifies(cand, muted, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertFalse(ok)
        self.assertEqual(reason, "normal_check_not_delivered")
        # ... and when there is no normal check at all.
        ok2, reason2 = qualifies(cand, None, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertFalse(ok2)
        self.assertEqual(reason2, "normal_check_not_delivered")

    def test_blocks_low_probability(self):
        # Condition 1: must be high probability of actually occurring.
        cand = _cand(probability=0.55)
        normal = NormalCheck(checkpoint="10M", delivered=True, side="YES")
        ok, reason = qualifies(cand, normal, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertFalse(ok)
        self.assertEqual(reason, "low_probability")
        # None probability (no learned data yet) is also blocked.
        ok2, reason2 = qualifies(_cand(probability=None), normal,
                                 min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertFalse(ok2)
        self.assertEqual(reason2, "low_probability")

    def test_blocks_unchanged_recommendation(self):
        # Condition 3: must differ from the normal check. Same side + same action.
        cand = _cand(new_side="YES", new_action="ENTRY_RECOMMENDED")
        normal = NormalCheck(checkpoint="10M", delivered=True, side="YES")
        ok, reason = qualifies(cand, normal, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertFalse(ok)
        self.assertEqual(reason, "unchanged")

    def test_blocks_repetitive(self):
        cand = _cand()
        normal = NormalCheck(checkpoint="10M", delivered=True, side="YES")
        already = {(cand.ticker, cand.checkpoint, "NO")}
        ok, reason = qualifies(cand, normal, min_probability=0.70, min_confidence=40.0, already_sent=already)
        self.assertFalse(ok)
        self.assertEqual(reason, "repetitive")

    def test_all_conditions_met_sends(self):
        cand = _cand()
        normal = NormalCheck(checkpoint="10M", delivered=True, side="YES", action="ENTRY_RECOMMENDED")
        ok, reason = qualifies(cand, normal, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_action_change_alone_qualifies(self):
        # Side unchanged but action flips entry -> exit: still a different action.
        cand = _cand(new_side="YES", new_action="exit now")
        normal = NormalCheck(checkpoint="10M", delivered=True, side="YES")
        ok, reason = qualifies(cand, normal, min_probability=0.70, min_confidence=40.0, already_sent=set())
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class MessageContentTest(unittest.TestCase):
    def test_combined_alert_has_all_required_fields(self):
        normal = NormalCheck(checkpoint="10M", delivered=True, asset="BTC",
                             side="YES", action="ENTRY_RECOMMENDED")
        msg = build_combined_alert("10M", normal, [_cand(), _cand(asset="ETH", ticker="KXETH-10M-1")])
        self.assertIn(ALERT_MARKER, msg)
        self.assertIn("10M", msg)                         # time interval
        self.assertIn("Probability: 80%", msg)            # probability
        self.assertIn("confidence 65%", msg)              # confidence
        self.assertIn("What changed:", msg)               # what changed
        self.assertIn("Original recommendation:", msg)    # original
        self.assertIn("New recommendation:", msg)         # new
        self.assertIn("Evidence:", msg)                   # evidence
        self.assertIn("YES → NO", msg)               # the flip direction
        # Combined: both assets in ONE message.
        self.assertIn("BTC", msg)
        self.assertIn("ETH", msg)


class DispatchGlueTest(unittest.TestCase):
    """Exercise the manager glue (_dispatch_manipulation_alerts) without the heavy
    __init__: per-interval filtering, the normal-check gate, single combined send,
    and dedup of repeated findings."""

    def _manager(self, candidates, normal_check):
        from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95
        mgr = CheckpointPolicyV95.__new__(CheckpointPolicyV95)
        mgr._manip_candidates = candidates
        mgr._normal_check = normal_check
        mgr._manip_alert_sent = set()
        return mgr

    class _Notifier:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return True

    def test_no_send_without_delivered_normal_check(self):
        mgr = self._manager([_cand()], {"10M": NormalCheck(checkpoint="10M", delivered=False)})
        notif = self._Notifier()
        s, f = mgr._dispatch_manipulation_alerts("10M", notif, now=1000.0)
        self.assertEqual((s, f), (0, 0))
        self.assertEqual(notif.sent, [])

    def test_sends_one_combined_alert_then_dedups(self):
        normal = {"10M": NormalCheck(checkpoint="10M", delivered=True, asset="BTC",
                                     side="YES", action="ENTRY_RECOMMENDED")}
        cands = [_cand(), _cand(asset="ETH", ticker="KXETH-10M-1")]
        mgr = self._manager(cands, normal)
        notif = self._Notifier()
        s, f = mgr._dispatch_manipulation_alerts("10M", notif, now=1000.0)
        self.assertEqual(s, 1)            # ONE combined message
        self.assertEqual(len(notif.sent), 1)
        self.assertIn("BTC", notif.sent[0])
        self.assertIn("ETH", notif.sent[0])
        # Same findings next cycle -> deduped, nothing sent.
        mgr._manip_candidates = [_cand(), _cand(asset="ETH", ticker="KXETH-10M-1")]
        s2, f2 = mgr._dispatch_manipulation_alerts("10M", notif, now=1001.0)
        self.assertEqual(s2, 0)
        self.assertEqual(len(notif.sent), 1)

    def test_only_processes_current_interval(self):
        normal = {"10M": NormalCheck(checkpoint="10M", delivered=True, side="YES")}
        # candidate is for 7M; dispatching 10M must ignore it.
        mgr = self._manager([_cand(checkpoint="7M")], normal)
        notif = self._Notifier()
        s, f = mgr._dispatch_manipulation_alerts("10M", notif, now=1000.0)
        self.assertEqual((s, f), (0, 0))


if __name__ == "__main__":
    unittest.main()
