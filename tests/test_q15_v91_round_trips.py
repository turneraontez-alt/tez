from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from q15_upgrade.checkpoint_v91 import CheckpointPolicyV91


class DisabledStore:
    enabled = False


def snapshot(asset="DOGE", ticker="DOGE-RT", seconds=900, p_yes=0.15):
    side = "NO" if p_yes < 0.5 else "YES"
    return {
        "asset": asset,
        "ticker": ticker,
        "seconds_remaining": seconds,
        "v9_calibrated_p_yes": p_yes,
        "calibrated_edge_side": side,
        "calibrated_edge_status": "READY",
        "calibrated_side_probability": max(p_yes, 1 - p_yes),
        "conservative_side_probability": max(p_yes, 1 - p_yes) - 0.05,
        "canonical_economics": {"executable_entry_ask_cents": 60.0},
        "trade_quality": {"required_edge_cents": 4.0},
    }


class V91RoundTripTests(unittest.TestCase):
    """The hot pre-enrich path must read predictions in ONE batched query
    instead of a freeze read-back plus two per-checkpoint reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        os.environ["Q15_V91_OBSERVATION_BUCKET_SECONDS"] = "10"
        os.environ["Q15_V91_ROLLING_WINDOW"] = "5"
        self.policy = CheckpointPolicyV91(DisabledStore(), self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        p = self.policy.persistence
        counts = {"get_prediction": 0, "get_predictions_for": 0, "write_prediction": 0}
        orig_gp = p.get_prediction
        orig_gpf = p.get_predictions_for
        orig_wp = p.write_prediction

        def gp(*a, **k):
            counts["get_prediction"] += 1
            return orig_gp(*a, **k)

        def gpf(*a, **k):
            counts["get_predictions_for"] += 1
            return orig_gpf(*a, **k)

        def wp(*a, **k):
            counts["write_prediction"] += 1
            return orig_wp(*a, **k)

        p.get_prediction = gp
        p.get_predictions_for = gpf
        p.write_prediction = wp
        return counts

    def test_pre_enrich_uses_single_batched_read_no_readback(self):
        counts = self._instrument()
        # 10M checkpoint exercises the freeze + prior-checkpoint comparison path.
        snap = snapshot(seconds=590)
        self.policy.pre_enrich_all({snap["asset"]: snap}, 1000.0)
        # Exactly one write (no read-back) and one batched read per asset; the
        # legacy per-checkpoint get_prediction calls are gone.
        self.assertEqual(counts["write_prediction"], 1)
        self.assertEqual(counts["get_predictions_for"], 1)
        self.assertEqual(counts["get_prediction"], 0)

    def test_batched_read_matches_per_checkpoint_reads(self):
        p = self.policy.persistence
        # Freeze a 15M and a 10M prediction for the same contract.
        for cp, side in (("15M", "NO"), ("10M", "NO")):
            p.write_prediction({
                "prediction_key": f"C|DOGE|{cp}|x",
                "contract_key": "C", "asset": "DOGE", "checkpoint": cp,
                "side": side, "p_yes": 0.12, "evidence_status": "OK",
                "rolling": {"k": 1}, "created_at": 1000.0,
            })
        batched = p.get_predictions_for("C", "DOGE", ("15M", "10M"))
        self.assertEqual(set(batched), {"15M", "10M"})
        for cp in ("15M", "10M"):
            single = p.get_prediction("C", "DOGE", cp)
            self.assertEqual(batched[cp]["side"], single["side"])
            self.assertEqual(batched[cp]["p_yes"], single["p_yes"])
            self.assertEqual(batched[cp].get("rolling"), single.get("rolling"))

    def test_missing_checkpoints_return_empty(self):
        p = self.policy.persistence
        self.assertEqual(p.get_predictions_for("NONE", "DOGE", ("15M", "10M")), {})
        self.assertEqual(p.get_predictions_for("C", "DOGE", ()), {})


if __name__ == "__main__":
    unittest.main()
