"""Synchronized-snapshot, Eastern-Time, and reset coverage for Shadow vs Yours.

Pins the three requirements added on top of the window-grading repair:
  * SIMULTANEOUS PREDICTIONS — both systems are recorded from one frozen snapshot
    per interval, and that shared snapshot_id is stamped on the paired row.
  * TIME ZONE — every visible timestamp renders in America/Detroit, DST-aware
    (EDT in summer, EST in winter); internal epoch/UTC is unchanged.
  * RESET — the visible comparison is bumped to challenger-v4; prior versions stay
    archived as PRE-SYNCHRONIZED-RESET and never mix in; the post-reset banner shows
    both systems at 0W–0L | N/A with the synchronization + time-zone lines.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.runner import ShadowRunner
from q15_upgrade import timez


def _cfg(tmp, **kw):
    base = dict(enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
                model_version="challenger-v4", refit_every=0, min_train_rows=5)
    base.update(kw)
    return ChallengerConfig().with_overrides(**base)


class TimeZoneTest(unittest.TestCase):
    def test_eastern_is_dst_aware_edt_and_est(self):
        # Summer instant -> EDT (UTC-4); winter instant -> EST (UTC-5). Never a
        # fixed offset.
        july = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc).timestamp()
        jan = datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(timez.tz_abbrev(july), "EDT")
        self.assertEqual(timez.tz_abbrev(jan), "EST")
        self.assertTrue(timez.fmt_eastern(july).endswith("EDT"))
        self.assertTrue(timez.fmt_eastern(jan).endswith("EST"))
        # 16:00 UTC -> 12:00 EDT in July, 11:00 EST in January.
        self.assertTrue(timez.fmt_eastern_hm(july).startswith("12:00"))
        self.assertTrue(timez.fmt_eastern_hm(jan).startswith("11:00"))

    def test_zone_name_is_iana_detroit(self):
        self.assertEqual(timez.TZ_NAME, "America/Detroit")

    def test_formatting_does_not_change_the_instant(self):
        # Display conversion must not move the underlying epoch (contract close /
        # interval timing stays UTC-correct internally).
        ts = datetime(2026, 6, 21, 18, 45, tzinfo=timezone.utc).timestamp()
        self.assertEqual(timez.to_eastern(ts).timestamp(), ts)


class SnapshotIdTest(unittest.TestCase):
    def test_both_systems_share_one_snapshot_id_on_the_paired_row(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        sid = "15M@1700000000"
        # One observe records BOTH systems (challenger pred + control prob) on one
        # row; the shared snapshot_id is stamped on it.
        r.observe(ticker="KXBTC", asset="BTC", checkpoint="15M", created_at=1_700_000_000,
                  close_time=1_700_000_900, control_prob_yes=0.7, snapshot_id=sid,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        row = r.ledger._conn.execute(
            "SELECT snapshot_id, control_prob_yes, challenger_prob_yes "
            "FROM shadow_predictions WHERE contract='KXBTC' AND checkpoint='15M'"
        ).fetchone()
        self.assertEqual(row["snapshot_id"], sid)
        # both systems' probabilities live on the same (same-snapshot) row
        self.assertIsNotNone(row["control_prob_yes"])
        self.assertIsNotNone(row["challenger_prob_yes"])

    def test_all_assets_in_one_interval_freeze_share_the_id(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        sid = "10M@1700000600"
        for a in ("BTC", "ETH", "SOL"):
            r.observe(ticker=f"KX{a}", asset=a, checkpoint="10M", created_at=1_700_000_600,
                      close_time=1_700_000_900, control_prob_yes=0.6, snapshot_id=sid,
                      features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        ids = [row["snapshot_id"] for row in r.ledger._conn.execute(
            "SELECT snapshot_id FROM shadow_predictions WHERE checkpoint='10M'")]
        self.assertEqual(set(ids), {sid})

    def test_record_prediction_threads_snapshot_id(self):
        # The production seam (V95Ledger.record_prediction) must accept snapshot_id.
        import inspect
        from q15_upgrade.ledger_v95 import V95Ledger
        self.assertIn("snapshot_id", inspect.signature(V95Ledger.record_prediction).parameters)


class ResetBannerTest(unittest.TestCase):
    def test_post_reset_banner_has_required_lines(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        msg = r.report_message().replace("<pre>", "").replace("</pre>", "")
        self.assertIn("Comparison reset:", msg)
        self.assertIn("Synchronization: Same snapshot and prediction time", msg)
        self.assertIn("Time zone: America/Detroit", msg)
        self.assertIn("Shadow: 0W–0L | N/A", msg)
        self.assertIn("Your System: 0W–0L | N/A", msg)
        # The reset timestamp carries an Eastern label (EDT or EST), not UTC.
        reset_line = next(l for l in msg.splitlines() if l.startswith("Comparison reset:"))
        self.assertTrue(reset_line.endswith("EDT") or reset_line.endswith("EST"),
                        f"reset line not Eastern-labelled: {reset_line!r}")
        self.assertNotIn("UTC", msg)

    def test_prior_versions_archived_not_mixed(self):
        tmp = tempfile.mkdtemp()
        cfg = _cfg(tmp)
        r = ShadowRunner(cfg)
        # A settled row under an OLD version stays archived and never scores into v4.
        r.ledger._conn.execute(
            "INSERT INTO shadow_predictions (created_at, model_version, asset, contract, "
            "checkpoint, close_time, control_prob_yes, challenger_prob_yes, official_result, "
            "native_sent) VALUES (1.0,'challenger-v3','BTC','OLD','10M',900,0.9,0.9,'YES',1)")
        r.ledger._conn.commit()
        self.assertEqual(r.ranked()["n_cases"], 0)
        self.assertIn("challenger-v3", r.ledger.archived_versions("challenger-v4"))
        msg = r.report_message()
        self.assertIn("PRE-SYNCHRONIZED-RESET", msg)

    def test_window_label_is_eastern(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        close = datetime(2026, 6, 21, 18, 45, tzinfo=timezone.utc).timestamp()  # 14:45 EDT
        for a, prob in (("BTC", 0.8), ("ETH", 0.3)):
            r.observe(ticker=f"KX{a}", asset=a, checkpoint="10M", created_at=close - 600,
                      close_time=close, control_prob_yes=prob, snapshot_id="10M@x",
                      features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
            r.mark_native_sent(f"KX{a}", "10M")
        for a in ("BTC", "ETH"):
            r.resolve(f"KX{a}", "10M", "YES", resolved_at=close + 1)
        msg = r.report_message().replace("<pre>", "").replace("</pre>", "")
        last = next(l for l in msg.splitlines() if l.startswith("LAST WINDOW"))
        self.assertIn("14:45 EDT", last)


if __name__ == "__main__":
    unittest.main()
