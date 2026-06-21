from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager


class Store:
    enabled = False


class Notifier:
    enabled = True

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)
        return True


def make_manager(settings=None):
    notifier = Notifier()
    # Dip alerts are OFF by default now (consolidated onto the unified panel);
    # this suite exercises the dip mechanism, so enable it unless a test overrides.
    mgr = TwoWindowFocusManager(
        Store(), notifier, None, None,
        settings if settings is not None else FocusSettings(dip_alert_enabled=True),
    )
    # Local-only claim so the test does not depend on a DB store.
    claimed = set()
    mgr._claim = lambda key: (key not in claimed) and (claimed.add(key) or True)  # type: ignore[assignment]
    return mgr, notifier


def snap(edge, side="YES", win=0.62, seconds=540, ask=41.0, ticker="BTC-T"):
    return {
        "ticker": ticker,
        "seconds_remaining": seconds,
        "calibrated_edge_side": side,
        "conservative_edge_after_costs_cents": edge,
        "calibrated_side_probability": win,
        "yes_ask": ask,
        "no_ask": ask,
    }


class DipAlertTest(unittest.TestCase):
    def test_fires_once_on_favorable_edge(self):
        mgr, notifier = make_manager()
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=7.5)}, 0.0)
        self.assertEqual(len(notifier.messages), 1)
        msg = notifier.messages[0]
        self.assertIn("DIP — BTC YES", msg)
        self.assertIn("edge +7.5", msg)
        self.assertIn("Read-only monitor", msg)

    def test_suppressed_while_armed_false(self):
        mgr, notifier = make_manager()
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=7.5)}, 0.0)
        # Still favorable, but trigger has not re-armed and cooldown not elapsed.
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=8.0)}, 1.0)
        self.assertEqual(len(notifier.messages), 1)

    def test_rearms_after_edge_recedes(self):
        s = FocusSettings(dip_alert_enabled=True)
        mgr, notifier = make_manager(s)
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=7.5)}, 0.0)
        # Edge recedes below the re-arm threshold -> trigger re-arms.
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=1.0)}, s.dip_alert_cooldown_seconds + 1)
        # New dip after cooldown -> second ping with a fresh claim key.
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0)}, s.dip_alert_cooldown_seconds + 2)
        self.assertEqual(len(notifier.messages), 2)

    def test_win_prob_floor_blocks(self):
        mgr, notifier = make_manager()
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0, win=0.40)}, 0.0)
        self.assertEqual(len(notifier.messages), 0)

    def test_below_edge_threshold_blocks(self):
        mgr, notifier = make_manager()
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=3.0)}, 0.0)
        self.assertEqual(len(notifier.messages), 0)

    def test_too_little_time_left_blocks(self):
        mgr, notifier = make_manager()
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0, seconds=30)}, 0.0)
        self.assertEqual(len(notifier.messages), 0)

    def test_disabled_flag_suppresses(self):
        mgr, notifier = make_manager(FocusSettings(dip_alert_enabled=False))
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0)}, 0.0)
        self.assertEqual(len(notifier.messages), 0)

    def test_claim_failure_advances_to_next_event(self):
        # Shared/permanent claim store: event 1 was already delivered by a peer
        # process or before a restart. Local state must still advance so a later
        # re-armed dip emits ...:2 instead of starving on the claimed ...:1 key.
        s = FocusSettings(dip_alert_enabled=True)
        notifier = Notifier()
        mgr = TwoWindowFocusManager(Store(), notifier, None, None, s)
        preclaimed = {"q15-dip:k:BTC:1"}
        sent = set()

        def claim(key):
            if key in preclaimed or key in sent:
                return False
            sent.add(key)
            return True

        mgr._claim = claim  # type: ignore[assignment]
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0)}, 0.0)
        self.assertEqual(len(notifier.messages), 0)  # claim of ...:1 failed
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=1.0)}, s.dip_alert_cooldown_seconds + 1)  # re-arm
        mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0)}, s.dip_alert_cooldown_seconds + 2)
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("q15-dip:k:BTC:2", sent)

    def test_per_cycle_cap(self):
        s = FocusSettings(dip_alert_enabled=True, dip_alert_max_per_cycle=2)
        mgr, notifier = make_manager(s)
        now = 0.0
        # Three separate dip events, each spaced past cooldown with a recede in between.
        for i in range(3):
            mgr._maybe_dip_alert("k", {"BTC": snap(edge=9.0)}, now)
            now += s.dip_alert_cooldown_seconds + 1
            mgr._maybe_dip_alert("k", {"BTC": snap(edge=1.0)}, now)  # recede -> re-arm
            now += 1
        self.assertEqual(len(notifier.messages), 2)


if __name__ == "__main__":
    unittest.main()
