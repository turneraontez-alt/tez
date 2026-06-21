"""Regression tests for time-authoritative checkpoint resolution (v9.5).

Root cause these lock down: the inherited ``_detect_checkpoint`` consults a
recursive snapshot key-walk and the buffered parent message text *before* its
time-based fallback. A parent message that merely contains the substring
``"15M"`` (e.g. the ``30M CHART CONTEXT — PRIOR 15M + CURRENT 15M`` header) or a
stale nested ``stage``/``checkpoint`` value pins the label to ``15M`` for the
whole cycle. Observed effect in production: every prediction recorded under 15M,
so 10M/7M never accumulated and the 10M/7M checkpoint alerts never fired.

``_resolve_checkpoint`` trusts ``seconds_remaining`` when present.
"""
import os
import unittest

from q15_upgrade.checkpoint_v95 import _resolve_checkpoint, _detect_checkpoint

# The exact parent header that triggered the live misdetection.
_CHART_CONTEXT_MSG = "30M CHART CONTEXT — PRIOR 15M + CURRENT 15M"


class TimeAuthoritativeCheckpointTest(unittest.TestCase):
    def setUp(self):
        # Ensure the flag default (ON) is in force regardless of the environment.
        self._saved = os.environ.pop("Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT"] = self._saved
        else:
            os.environ.pop("Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT", None)

    def test_time_wins_over_15m_chart_context_message(self):
        # 560s remaining == the 10-minute window. The legacy detector returns the
        # WRONG label here (15M) purely because the message contains "15M".
        self.assertEqual(
            _detect_checkpoint({"E": {"seconds_remaining": 560}}, [_CHART_CONTEXT_MSG]),
            "15M",
        )
        # The fix: time is authoritative -> 10M.
        self.assertEqual(
            _resolve_checkpoint({"E": {"seconds_remaining": 560}}, [_CHART_CONTEXT_MSG], 0.0),
            "10M",
        )

    def test_time_wins_over_stale_nested_key(self):
        snaps = {"E": {"seconds_remaining": 560, "meta": {"stage": "15M"}}}
        self.assertEqual(_detect_checkpoint(snaps, []), "15M")  # legacy bug
        self.assertEqual(_resolve_checkpoint(snaps, [], 0.0), "10M")  # fixed

    def test_boundaries_match_time_fallback(self):
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 800}}, [_CHART_CONTEXT_MSG], 0.0), "15M")
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 560}}, [_CHART_CONTEXT_MSG], 0.0), "10M")
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 420}}, [_CHART_CONTEXT_MSG], 0.0), "7M")
        # Exact boundary values: >=660 -> 15M, >=480 -> 10M.
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 660}}, [], 0.0), "15M")
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 659}}, [], 0.0), "10M")
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 480}}, [], 0.0), "10M")
        self.assertEqual(_resolve_checkpoint({"E": {"seconds_remaining": 479}}, [], 0.0), "7M")

    def test_uses_longest_remaining_across_assets(self):
        # Mirrors _detect_checkpoint's max() so a single laggard market does not
        # drag the whole cycle into an earlier checkpoint.
        snaps = {"A": {"seconds_remaining": 800}, "B": {"seconds_remaining": 560}}
        self.assertEqual(_resolve_checkpoint(snaps, [], 0.0), "15M")

    def test_falls_back_to_detector_when_no_time(self):
        # No seconds_remaining anywhere -> defer to the heuristic detector, which
        # still reads explicit labels / message text.
        self.assertEqual(_resolve_checkpoint({"E": {"checkpoint": "7M"}}, [], 0.0), "7M")
        self.assertEqual(_resolve_checkpoint({}, ["7M FINAL CHECK"], 0.0), "7M")

    def test_flag_off_restores_legacy_behaviour(self):
        os.environ["Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT"] = "false"
        try:
            # With the flag off we delegate entirely to _detect_checkpoint, so the
            # buggy 15M-context message wins again (legacy parity, intentional).
            self.assertEqual(
                _resolve_checkpoint({"E": {"seconds_remaining": 560}}, [_CHART_CONTEXT_MSG], 0.0),
                "15M",
            )
        finally:
            os.environ.pop("Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT", None)


if __name__ == "__main__":
    unittest.main()
