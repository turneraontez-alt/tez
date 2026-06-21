from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.runner import ShadowRunner, _build_snapshot
import notifications.notifier as notifier_mod


def _cfg(tmp):
    return ChallengerConfig().with_overrides(
        enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
        refit_every=5, min_train_rows=20, model_version="challenger-test",
    )


class BuildSnapshotTest(unittest.TestCase):
    def test_maps_signals_and_quote(self):
        snap = _build_snapshot(
            {"momentum": 0.2, "flow": {"value": -0.1, "quality": 0.8}},
            {"yes_bid_cents": 40, "yes_ask_cents": 42, "no_bid_cents": 56, "no_ask_cents": 58},
        )
        self.assertEqual(snap["feature_values"]["momentum"], 0.2)
        self.assertEqual(snap["feature_values"]["flow"], -0.1)  # unwrapped from {value,quality}
        self.assertEqual(snap["yes_ask"], 42)


class RunnerTest(unittest.TestCase):
    def test_observe_resolve_and_report(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # record + resolve 12 contracts across two 15-min windows
        for i in range(12):
            tkr = f"KXBTC15M-{i}"
            r.observe(ticker=tkr, asset="BTC", checkpoint="10M",
                      created_at=1_700_000_000 + i * 60,
                      close_time=1_700_000_900 + i * 60, control_prob_yes=0.55,
                      features={"momentum": 0.1, "flow": -0.05, "book": 0.0},
                      quote={"yes_bid_cents": 40, "yes_ask_cents": 42,
                             "no_bid_cents": 56, "no_ask_cents": 58})
            r.mark_native_sent(tkr, "10M")  # simulate the panel actually delivering
            r.resolve(tkr, "10M", "YES" if i % 2 == 0 else "NO",
                      resolved_at=1_700_000_900 + i * 60)
        cmp = r.comparison()
        self.assertEqual(cmp["overall"]["n"], 12)
        self.assertIsNotNone(cmp["overall"]["challenger_accuracy"])
        # current (native) accuracy is populated because each was marked delivered
        self.assertIsNotNone(cmp["overall"]["current_accuracy"])
        # and an UNDELIVERED prediction stays out of the visible native record
        r.observe(ticker="UNSENT", asset="BTC", checkpoint="10M", created_at=1_700_000_000,
                  close_time=1_700_000_900, control_prob_yes=0.9,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        r.resolve("UNSENT", "10M", "YES", resolved_at=1_700_000_900)
        self.assertEqual(r.comparison()["overall"]["n"], 13)  # row exists (background)
        self.assertIn("10M", cmp["by_checkpoint"])
        msg = r.report_message()
        self.assertIn("CHALLENGER SHADOW", msg)
        self.assertIn("Shadow", msg)

    def test_pending_report_set_on_new_window(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        r.observe(ticker="C1", asset="BTC", checkpoint="10M", created_at=1_700_000_000,
                  close_time=1_700_000_900, control_prob_yes=0.6,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        r.resolve("C1", "10M", "YES", resolved_at=1_700_000_900)
        first = r.drain_report()
        self.assertIsNotNone(first)
        self.assertIsNone(r.drain_report())  # drained once

    def test_observe_never_raises_on_bad_input(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # garbage features/quote must not raise (production safety)
        r.observe(ticker="X", asset="BTC", checkpoint="10M", created_at=0.0,
                  close_time=None, control_prob_yes=None, features=None, quote=None)


class NotifierBypassTest(unittest.TestCase):
    def test_challenger_report_not_suppressed(self):
        # Even under balanced level, the challenger report header is delivered.
        os.environ.pop("Q15_ALERT_LEVEL", None)
        msg = "CHALLENGER SHADOW — accuracy\nbody"
        self.assertFalse(notifier_mod.should_suppress_alert(msg, level="balanced")
                         and notifier_mod._is_challenger_report(msg) is False)
        self.assertTrue(notifier_mod._is_challenger_report(msg))


class RankedComparisonTest(unittest.TestCase):
    def _insert(self, led, *, asset, checkpoint, close, chal, ctrl, official,
                mv="challenger-test", native_sent=1):
        # native_sent defaults to 1 here so these rows represent delivered ("sent")
        # Your-System predictions; sent-gating tests pass native_sent=0 explicitly.
        led.ledger._conn.execute(
            "INSERT INTO shadow_predictions (created_at, model_version, asset, contract, "
            "checkpoint, close_time, control_prob_yes, challenger_prob_yes, official_result, "
            "native_sent) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1.0, mv, asset, f"{asset}-{checkpoint}-{int(close)}", checkpoint, close, ctrl, chal,
             official, native_sent),
        )
        led.ledger._conn.commit()

    def test_per_rank_scoring_no_double_count(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # One case (10M, close=1000) with 3 assets, controlled probs/outcomes.
        # challenger ranks by |p-0.5|: ETH(.4) BTC(.3) SOL(.1)
        # native     ranks by |p-0.5|: BTC(.35) SOL(.10) ETH(.05)
        self._insert(r, asset="ETH", checkpoint="10M", close=1000, chal=0.9, ctrl=0.55, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.2, ctrl=0.85, official="NO")
        self._insert(r, asset="SOL", checkpoint="10M", close=1000, chal=0.6, ctrl=0.40, official="YES")
        rk = r.ranked(top_k=3)
        self.assertEqual(rk["n_cases"], 1)
        # challenger: P1 ETH YES vs YES ok, P2 BTC NO vs NO ok, P3 SOL YES vs YES ok -> 3/3
        self.assertEqual(rk["challenger"]["rank1"], {"correct": 1, "wrong": 0, "accuracy": 1.0})
        self.assertEqual(rk["challenger"]["overall"]["correct"], 3)
        self.assertEqual(rk["challenger"]["overall"]["accuracy"], 1.0)
        # native: P1 BTC YES vs NO x, P2 SOL NO vs YES x, P3 ETH YES vs YES ok -> 1/3
        self.assertEqual(rk["native"]["rank1"], {"correct": 0, "wrong": 1, "accuracy": 0.0})
        self.assertEqual(rk["native"]["overall"]["correct"], 1)
        self.assertAlmostEqual(rk["native"]["overall"]["accuracy"], 1 / 3, places=3)
        # report renders with both models + verdict, in the new ranked format
        msg = r.report_message()
        self.assertIn("ALL-TIME RANK RESULTS", msg)
        self.assertIn("#1 Shadow:", msg)
        self.assertIn("Shadow ahead", msg)
        # only the labelled tick symbols — no bare ok/X/+/- marks
        self.assertNotIn(" ok", msg)
        self.assertTrue(any(s in msg for s in ("✓", "✗")))

    def test_report_uses_only_check_symbols_and_all_sections(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # one settled case at 10M so the report has data to render.
        self._insert(r, asset="SOL", checkpoint="10M", close=3000, chal=0.8, ctrl=0.7, official="YES")
        msg = r.report_message()
        for header in ("CHALLENGER SHADOW vs YOUR SYSTEM", "Shadow test · read-only · never trades",
                       "Comparison reset:", "LAST WINDOW", "END-RESULT CALL · 15M, 10M & 7M",
                       "ALL-TIME RANK RESULTS", "SHADOW RECORD", "YOUR SYSTEM RECORD",
                       "15M TOTAL", "10M TOTAL", "7M TOTAL"):
            self.assertIn(header, msg)
        # ✓/✗/— only — never a bare +, lone X marker, or "ok"
        self.assertNotIn(" ok", msg)
        self.assertIn("✓", msg)
        # all three intervals appear in the all-time record (7M with 0W–0L | N/A)
        self.assertIn("7M\n#1: 0W–0L | N/A", msg.replace("<pre>", "").replace("</pre>", ""))

    def test_ranked_by_checkpoint_records_per_rank_and_interval(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # a 15M case (SOL right at #1 for shadow) and a 7M case; 10M stays empty.
        self._insert(r, asset="SOL", checkpoint="15M", close=4000, chal=0.9, ctrl=0.9, official="YES")
        self._insert(r, asset="BTC", checkpoint="7M", close=4000, chal=0.2, ctrl=0.8, official="NO")
        rbc = r.ledger.ranked_by_checkpoint(model_version="challenger-test", top_k=3)
        self.assertEqual(set(rbc["by_checkpoint"]), {"15M", "10M", "7M"})
        # 15M #1 shadow correct (SOL YES==YES); native also YES==YES
        self.assertEqual(rbc["by_checkpoint"]["15M"]["challenger"]["rank1"]["correct"], 1)
        self.assertEqual(rbc["by_checkpoint"]["15M"]["challenger"]["total"]["accuracy"], 1.0)
        # 7M #1 shadow NO==NO correct; native YES!=NO wrong
        self.assertEqual(rbc["by_checkpoint"]["7M"]["challenger"]["rank1"]["correct"], 1)
        self.assertEqual(rbc["by_checkpoint"]["7M"]["native"]["rank1"]["wrong"], 1)
        # 10M had no case -> 0W-0L / accuracy None
        self.assertEqual(rbc["by_checkpoint"]["10M"]["challenger"]["total"],
                         {"correct": 0, "wrong": 0, "accuracy": None})

    def test_reset_marker_stamped_once_and_archives_pre_reset(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        first = r.ledger.reset_marker("challenger-test")
        # stamped once and stable across calls
        self.assertEqual(first, r.ledger.reset_marker("challenger-test"))
        # a row under a DIFFERENT (pre-reset) model_version is archived, not scored
        self._insert(r, asset="BTC", checkpoint="10M", close=900, chal=0.9, ctrl=0.9,
                     official="YES", mv="challenger-v1")
        rk = r.ranked()
        self.assertEqual(rk["n_cases"], 0)  # pre-reset row excluded from the record

    def test_default_model_version_is_v4_synchronized_reset(self):
        # The default model_version is the reset key: pinning it makes an accidental
        # edit (or a forgotten reset-on-deploy) fail loudly rather than silently
        # keep scoring the old comparison. v4 = the synchronized-snapshot reset.
        self.assertEqual(ChallengerConfig.from_env().model_version, "challenger-v4")

    def test_v4_reset_archives_prior_versions(self):
        # Fresh v4 comparison: settled rows from prior versions (v1/v2/v3) stay
        # archived in the same file and never enter the new visible record.
        tmp = tempfile.mkdtemp()
        cfg = _cfg(tmp).with_overrides(model_version="challenger-v4")
        r = ShadowRunner(cfg)
        self._insert(r, asset="BTC", checkpoint="10M", close=900, chal=0.9, ctrl=0.9,
                     official="YES", mv="challenger-v1")
        self._insert(r, asset="ETH", checkpoint="15M", close=900, chal=0.9, ctrl=0.9,
                     official="YES", mv="challenger-v3")
        # No v4 rows yet -> the visible record is empty across every interval.
        rk = r.ledger.ranked_comparison(model_version="challenger-v4")
        self.assertEqual(rk["n_cases"], 0)
        rbc = r.ledger.ranked_by_checkpoint(model_version="challenger-v4")
        for cp in ("15M", "10M", "7M"):
            self.assertEqual(rbc["by_checkpoint"][cp]["challenger"]["total"]["accuracy"], None)
            self.assertEqual(rbc["by_checkpoint"][cp]["native"]["total"]["accuracy"], None)
        # The pre-reset rows are still in the file (archived, not deleted) and the
        # ledger reports them as the PRE-SYNCHRONIZED-RESET archive.
        for mv in ("challenger-v1", "challenger-v3"):
            (cnt,) = r.ledger._conn.execute(
                "SELECT COUNT(*) FROM shadow_predictions WHERE model_version=?", (mv,)
            ).fetchone()
            self.assertEqual(cnt, 1)
        self.assertEqual(r.ledger.archived_versions("challenger-v4"),
                         ["challenger-v1", "challenger-v3"])
        # A new post-reset prediction is what starts the fresh comparison.
        self._insert(r, asset="SOL", checkpoint="10M", close=1800, chal=0.8, ctrl=0.8,
                     official="YES", mv="challenger-v4")
        self.assertEqual(r.ledger.ranked_comparison(model_version="challenger-v4")["n_cases"], 1)

    def test_empty_report_shows_reset_and_zero(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        msg = r.report_message()
        self.assertIn("Comparison reset:", msg)
        self.assertIn("0W–0L | N/A", msg)

    def test_end_result_section(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # latest window: BTC settles YES, called right at both checkpoints by the
        # shadow but missed at 15M by native.
        self._insert(r, asset="BTC", checkpoint="15M", close=2000, chal=0.7, ctrl=0.45, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=2000, chal=0.8, ctrl=0.62, official="YES")
        er = r.ledger.latest_window_end_results(model_version="challenger-test")
        self.assertEqual(len(er["assets"]), 1)
        a = er["assets"][0]
        self.assertEqual(a["official"], "YES")
        self.assertEqual(a["checkpoints"]["15M"]["challenger"], ("YES", True))
        self.assertEqual(a["checkpoints"]["15M"]["native"], ("NO", False))
        self.assertEqual(a["checkpoints"]["10M"]["native"], ("YES", True))
        msg = r.report_message()
        self.assertIn("END-RESULT CALL", msg)

    def test_distinct_cases_by_checkpoint_and_window(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # same close, different checkpoints -> 2 cases; same checkpoint diff close -> +1
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.9, ctrl=0.9, official="YES")
        self._insert(r, asset="BTC", checkpoint="15M", close=1000, chal=0.9, ctrl=0.9, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=1900, chal=0.9, ctrl=0.9, official="NO")
        rk = r.ranked()
        self.assertEqual(rk["n_cases"], 3)

    def test_unsent_native_excluded_but_shadow_counts(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # Two assets in one 10M case; native NEVER delivered (background only).
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.9, ctrl=0.9,
                     official="YES", native_sent=0)
        self._insert(r, asset="SOL", checkpoint="10M", close=1000, chal=0.8, ctrl=0.8,
                     official="YES", native_sent=0)
        rk = r.ranked()  # sent-gated (default)
        # Shadow (read-only test) counts both; Your System counts none (unsent).
        self.assertEqual(rk["challenger"]["overall"]["correct"], 2)
        self.assertEqual(rk["native"]["overall"], {"correct": 0, "wrong": 0, "accuracy": None})
        rbc = r.ledger.ranked_by_checkpoint(model_version="challenger-test", native_sent_only=True)
        self.assertEqual(rbc["by_checkpoint"]["10M"]["native"]["total"]["accuracy"], None)
        self.assertEqual(rbc["by_checkpoint"]["10M"]["challenger"]["total"]["correct"], 2)
        # End-result call: native reads — (no official prediction), shadow shows the call.
        er = r.ledger.latest_window_end_results(model_version="challenger-test")
        a = next(x for x in er["assets"] if x["asset"] == "BTC")
        self.assertIsNone(a["checkpoints"]["10M"]["native"])
        self.assertEqual(a["checkpoints"]["10M"]["challenger"], ("YES", True))
        # Legacy count-all (sent_only OFF) brings the native rows back.
        rk_all = r.ledger.ranked_comparison(model_version="challenger-test", native_sent_only=False)
        self.assertEqual(rk_all["native"]["overall"]["correct"], 2)

    def test_mark_native_sent_promotes_to_visible_record(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.2, ctrl=0.9,
                     official="YES", native_sent=0)
        self.assertEqual(r.ranked()["native"]["overall"]["correct"], 0)  # unsent
        # delivery before close marks it sent -> now visible (and correct: 0.9->YES==YES)
        self.assertTrue(r.mark_native_sent("BTC-10M-1000", "10M"))
        self.assertEqual(r.ranked()["native"]["overall"]["correct"], 1)
        # idempotent: a second mark is a no-op, count unchanged
        self.assertFalse(r.mark_native_sent("BTC-10M-1000", "10M"))
        self.assertEqual(r.ranked()["native"]["overall"]["correct"], 1)

    def test_migration_adds_native_sent_to_legacy_db(self):
        import sqlite3
        from q15_upgrade.challenger.ledger import ShadowLedger
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.sqlite3")
        # Pre-create a shadow_predictions table WITHOUT native_sent (old schema).
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE shadow_predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "model_version TEXT, contract TEXT, checkpoint TEXT, official_result TEXT)")
        conn.execute("INSERT INTO shadow_predictions (model_version, contract, checkpoint) "
                     "VALUES ('challenger-test','C1','10M')")
        conn.commit(); conn.close()
        led = ShadowLedger(path)  # __init__ migrates
        cols = {row["name"] for row in led._conn.execute("PRAGMA table_info(shadow_predictions)")}
        self.assertIn("native_sent", cols)
        # existing row defaulted to 0 (unsent) and mark flips it
        self.assertTrue(led.mark_native_sent("C1", "10M", model_version="challenger-test"))


class GatingTest(unittest.TestCase):
    def test_disabled_runner_is_none(self):
        import q15_upgrade.challenger.runner as rm
        rm._runner = None
        rm._enabled_cache = False
        self.assertIsNone(rm.get_runner())


if __name__ == "__main__":
    unittest.main()
