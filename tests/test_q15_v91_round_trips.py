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
        names = [
            "get_prediction", "get_predictions_for", "get_predictions_for_pairs",
            "write_prediction", "write_predictions",
            "insert_observation", "insert_observations",
            "recent_observations_for_pairs",
        ]
        counts = {n: 0 for n in names}
        for n in names:
            orig = getattr(p, n)

            def make(fn, key):
                def wrapped(*a, **k):
                    counts[key] += 1
                    return fn(*a, **k)
                return wrapped

            setattr(p, n, make(orig, n))
        return counts

    def test_pre_enrich_batches_writes_and_reads(self):
        counts = self._instrument()
        # 10M checkpoint exercises the freeze + prior-checkpoint comparison path.
        snap = snapshot(seconds=590)
        self.policy.pre_enrich_all({snap["asset"]: snap}, 1000.0)
        # Bulk methods only: one observation insert, one prediction write, one
        # batched prediction read. The legacy per-call methods are unused.
        self.assertEqual(counts["insert_observations"], 1)
        self.assertEqual(counts["write_predictions"], 1)
        self.assertEqual(counts["get_predictions_for_pairs"], 1)
        self.assertEqual(counts["insert_observation"], 0)
        self.assertEqual(counts["write_prediction"], 0)
        self.assertEqual(counts["get_prediction"], 0)
        self.assertEqual(counts["get_predictions_for"], 0)

    def test_many_assets_still_one_round_trip_each(self):
        counts = self._instrument()
        snaps = {
            a: snapshot(asset=a, ticker=f"{a}-RT", seconds=590, p_yes=0.15)
            for a in ("BTC", "ETH", "SOL")
        }
        self.policy.pre_enrich_all(snaps, 1000.0)
        # 3 assets, still ONE insert / ONE write / ONE prediction read, and now
        # ONE batched rolling-window read instead of one per asset.
        self.assertEqual(counts["insert_observations"], 1)
        self.assertEqual(counts["write_predictions"], 1)
        self.assertEqual(counts["get_predictions_for_pairs"], 1)
        self.assertEqual(counts["recent_observations_for_pairs"], 1)

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

    def test_pairs_read_matches_per_pair_reads(self):
        p = self.policy.persistence
        rows = []
        for contract, asset, cp in (("C1", "BTC", "15M"), ("C1", "BTC", "10M"), ("C2", "ETH", "15M")):
            rows.append({
                "prediction_key": f"{contract}|{asset}|{cp}|x",
                "contract_key": contract, "asset": asset, "checkpoint": cp,
                "side": "NO", "p_yes": 0.2, "evidence_status": "OK",
                "rolling": {"k": cp}, "created_at": 1000.0,
            })
        p.write_predictions(rows)  # bulk write
        pairs = [("C1", "BTC"), ("C2", "ETH")]
        batched = p.get_predictions_for_pairs(pairs, ("15M", "10M"))
        # Grouped result equals per-pair get_predictions_for.
        for pair in pairs:
            self.assertEqual(
                batched.get(pair, {}).keys(),
                p.get_predictions_for(pair[0], pair[1], ("15M", "10M")).keys(),
            )
        self.assertEqual(set(batched[("C1", "BTC")]), {"15M", "10M"})
        self.assertEqual(set(batched[("C2", "ETH")]), {"15M"})

    def test_missing_checkpoints_return_empty(self):
        p = self.policy.persistence
        self.assertEqual(p.get_predictions_for("NONE", "DOGE", ("15M", "10M")), {})
        self.assertEqual(p.get_predictions_for("C", "DOGE", ()), {})
        self.assertEqual(p.get_predictions_for_pairs([], ("15M",)), {})
        self.assertEqual(p.get_predictions_for_pairs([("C", "DOGE")], ()), {})


if __name__ == "__main__":
    unittest.main()
