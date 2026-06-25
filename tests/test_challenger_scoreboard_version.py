"""R1 — the shadow scoreboard visibility fix.

ShadowLedger reporting methods used to default model_version to the dead literal
"challenger-v1". Once the live version advanced to "challenger-v5", a bare
scoreboard() call (as tools/learning_export.py makes) matched zero rows and
reported resolved=0 — hiding the challenger's entire resolved YES/NO record from
every review. These tests pin the fix: a no-arg scoreboard() resolves to the live
ChallengerConfig().model_version and sees the rows; the stale literal sees none.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.ledger import ShadowLedger, _DEFAULT_MODEL_VERSION


def _seed(path: str, model_version: str, n: int) -> None:
    """Insert n resolved shadow rows stamped with model_version."""
    led = ShadowLedger(path)
    conn = led._conn
    now = time.time()
    for i in range(n):
        # YES on even i, NO on odd; challenger probability on the correct side so
        # the scoreboard has real (not degenerate) inputs to score.
        result = "YES" if i % 2 == 0 else "NO"
        prob = 0.7 if result == "YES" else 0.3
        conn.execute(
            "INSERT INTO shadow_predictions "
            "(created_at, model_version, asset, contract, checkpoint, "
            " control_prob_yes, challenger_raw_prob_yes, challenger_prob_yes, "
            " side, official_result, resolved_at, hypothetical_pnl_cents) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, model_version, "BTC", f"C{i}", "10M",
             prob, prob, prob, result, result, now, 1.0),
        )
    conn.commit()
    led.close()


class ShadowScoreboardVersionTest(unittest.TestCase):
    def test_default_resolves_to_live_version(self):
        self.assertEqual(_DEFAULT_MODEL_VERSION, ChallengerConfig().model_version)

    def test_bare_scoreboard_sees_live_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "shadow.sqlite3")
            _seed(path, _DEFAULT_MODEL_VERSION, 6)
            led = ShadowLedger(path)
            try:
                # The whole point of R1: no-arg scoreboard() now finds the rows.
                self.assertEqual(led.scoreboard()["resolved"], 6)
                # The old dead literal still matches nothing — proving the bare
                # call was previously scoring against an empty set.
                self.assertEqual(led.scoreboard("challenger-v1")["resolved"], 0)
                # Explicit live version agrees with the bare default.
                self.assertEqual(
                    led.scoreboard(_DEFAULT_MODEL_VERSION)["resolved"], 6)
            finally:
                led.close()

    def test_rows_under_a_different_version_are_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "shadow.sqlite3")
            _seed(path, "challenger-v1", 4)  # stale rows only
            led = ShadowLedger(path)
            try:
                # Bare default targets the LIVE version, so legacy-only rows
                # correctly report resolved=0 (no silent cross-version mixing).
                self.assertEqual(led.scoreboard()["resolved"], 0)
                self.assertEqual(led.scoreboard("challenger-v1")["resolved"], 4)
            finally:
                led.close()


if __name__ == "__main__":
    unittest.main()
