"""Exact-text locks for the V9.5 checkpoint panel and end-of-cycle recap.

The live checkpoint panel is forward-looking (manipulation + prediction + entry
guidance, NO record) and must make a side flip + WATCH state unmistakable; the
recap is the single close-out (per-interval hit/miss + flips + running totals).
"""
import unittest

from notifications import panels_v95 as P


class CheckpointPanelTest(unittest.TestCase):
    def test_keeps_marker_and_single_pre_panel(self):
        msg = P.build_checkpoint_panel(
            checkpoint="10M", asset="BTC", side="NO", probability=0.71,
            entry_state=P.WATCH,
        )
        self.assertIn("V9.5 CHECK", msg)              # suppression/formatter marker
        self.assertEqual(msg.count("<pre>"), 1)
        self.assertEqual(msg.count("</pre>"), 1)
        self.assertTrue(msg.split("\n", 1)[0].startswith("🔎 <b>V9.5 CHECK — 10M · BTC</b>"))

    def test_flip_is_unmistakable(self):
        msg = P.build_checkpoint_panel(
            checkpoint="10M", asset="BTC", side="YES", probability=0.69,
            prior_side="NO", prior_checkpoint="15M", entry_state=P.WAIT,
            entry={"current_price": 64, "entry_low": 58, "entry_high": 61,
                   "max_price": 62, "trigger": "rejection wick or momentum turn"},
        )
        self.assertIn("Was: NO at 15M  ⚠️ FLIPPED", msg)
        self.assertIn("⏳ ENTRY: WAIT FOR BETTER PRICE", msg)
        self.assertIn("Recommended entry: 58¢–61¢", msg)
        self.assertIn("Maximum price: 62¢", msg)

    def test_unchanged_prior_side(self):
        msg = P.build_checkpoint_panel(
            checkpoint="7M", asset="ETH", side="YES", probability=0.66,
            prior_side="YES", prior_checkpoint="10M", entry_state=P.ENTRY_RECOMMENDED,
        )
        self.assertIn("Was: YES at 10M (unchanged)", msg)
        self.assertNotIn("FLIPPED", msg)

    def test_first_read_omits_prior_line(self):
        msg = P.build_checkpoint_panel(
            checkpoint="15M", asset="SOL", side="NO", probability=0.6,
            entry_state=P.NO_ENTRY,
        )
        self.assertNotIn("Was:", msg)
        self.assertIn("🛑 ENTRY: NO ENTRY", msg)

    def test_watch_state_is_clearly_not_an_entry(self):
        msg = P.build_checkpoint_panel(
            checkpoint="10M", asset="BTC", side="NO", probability=0.58,
            entry_state=P.WATCH, entry={"reason": "directional confidence not yet sufficient"},
        )
        self.assertIn("👁 ENTRY: WATCH ONLY", msg)
        self.assertIn("Reason: directional confidence not yet sufficient", msg)
        self.assertNotIn("ENTER NOW", msg)
        self.assertNotIn("RECOMMENDED", msg)

    def test_manipulation_block_rendered(self):
        msg = P.build_checkpoint_panel(
            checkpoint="10M", asset="BTC", side="YES", probability=0.69,
            manipulation={"risk": 68, "level": "HIGH", "type": "Downward sweep",
                          "direction_after": "YES", "entry_effect": "WAIT"},
            entry_state=P.WAIT,
        )
        for needle in ("⚠️ MANIPULATION", "Risk: 68% HIGH", "Type: Downward sweep",
                       "Direction after: YES", "Entry effect: WAIT"):
            self.assertIn(needle, msg)

    def test_no_manipulation_shows_clear(self):
        msg = P.build_checkpoint_panel(
            checkpoint="10M", asset="BTC", side="YES", probability=0.69,
            entry_state=P.ENTER_NOW,
        )
        self.assertIn("✅ MANIPULATION: none detected", msg)
        self.assertIn("🟢 ENTRY: ENTER NOW", msg)


class CycleRecapTest(unittest.TestCase):
    def _official(self):
        def wl(r, w, acc, low=False):
            return {"right": r, "wrong": w, "accuracy": acc, "low_n": low}
        grp = lambda y, n, t: {"yes": y, "no": n, "total": t}
        return {
            "available": True,
            "15M": grp(wl(18, 7, 0.72), wl(21, 9, 0.70), wl(39, 16, 0.709)),
            "10M": grp(wl(24, 6, 0.80), wl(27, 8, 0.771), wl(51, 14, 0.785)),
            "7M": grp(wl(20, 5, 0.80), wl(22, 6, 0.786), wl(42, 11, 0.792)),
            "entry": grp(wl(14, 4, 0.778), wl(17, 5, 0.773), wl(31, 9, 0.775)),
            "manipulation": wl(22, 8, 0.733),
        }

    def test_recap_shows_per_interval_result_and_running_totals(self):
        msg = P.build_cycle_recap(
            asset="BTC", close_label="14:45", result="YES",
            intervals=[{"interval": "15M", "side": "NO", "hit": False},
                       {"interval": "10M", "side": "YES", "hit": True},
                       {"interval": "7M", "side": "YES", "hit": True}],
            flips="NO → YES at 10M (1)",
            entry_result={"decision": "ENTRY RECOMMENDED", "checkpoint": "10M",
                          "outcome": "WIN", "cents": 35},
            manipulation_result={"flagged": True, "type": "Downward sweep", "correct": True},
            official=self._official(),
        )
        self.assertIn("🏁 <b>CYCLE CLOSED — BTC 14:45</b>", msg)
        self.assertIn("Result: YES", msg)
        self.assertIn("15M:  NO  ✗", msg)
        self.assertIn("10M:  YES ✓", msg)
        self.assertIn("Flips: NO → YES at 10M (1)", msg)
        self.assertIn("Predictability: 2/3 correct", msg)
        self.assertIn("Entry: ENTRY RECOMMENDED @10M → WIN +35¢", msg)
        self.assertIn("Manipulation: Downward sweep → correct", msg)
        self.assertIn("— RUNNING RECORD —", msg)
        self.assertIn("10M   Y 24-6  N 27-8  T 51-14 78.5%", msg)
        self.assertIn("MANIP 22-8 73.3%", msg)

    def test_low_sample_marked(self):
        official = self._official()
        official["7M"]["total"]["low_n"] = True
        msg = P.build_cycle_recap(
            asset="ETH", close_label="15:00", result="NO",
            intervals=[{"interval": "10M", "side": "NO", "hit": True}],
            official=official,
        )
        self.assertIn("(low)", msg)
        self.assertIn("Predictability: 1/1 correct", msg)

    def test_recap_without_official_still_renders(self):
        msg = P.build_cycle_recap(
            asset="SOL", close_label="15:15", result="YES",
            intervals=[{"interval": "10M", "side": "YES", "hit": True}],
        )
        self.assertIn("Result: YES", msg)
        self.assertNotIn("RUNNING RECORD", msg)


if __name__ == "__main__":
    unittest.main()
