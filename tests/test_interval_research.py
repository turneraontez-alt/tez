"""Interval-timing research collector + economics (default-OFF, read-only)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.interval_research import config as cfg_mod
from q15_upgrade.interval_research.config import IntervalResearchConfig, INTERVAL_MARKS, INTERVAL_ROLES
from q15_upgrade.interval_research.ledger import IntervalResearchLedger
from q15_upgrade.interval_research.runner import IntervalResearchRunner, get_runner, reset_runner
from q15_upgrade.interval_research import economics as econ


class _Canon:
    def __init__(self, ticker, seconds_remaining, settlement_time=1_000_000.0):
        self.ticker = ticker
        self.seconds_remaining = seconds_remaining
        self.settlement_time = settlement_time


def _analysis(side="NO", ask=61.0, available=True, decision="WATCH_PRICE", edge=4.0):
    return {
        "prediction_available": available,
        "prediction_side": side,
        "raw_yes_probability": 0.45, "yes_probability": 0.40,
        "conservative_probability": 0.38,
        "flip_risk": {"score": 22.0}, "manipulation": {"suspected": True, "score": 0.33},
        "regime": {"distance_sigma": 0.18}, "shadow_signals": {"prediction_stability": 0.5},
        "data_quality": 0.9, "trade_decision": decision, "net_edge_cents": edge,
        "entry_ask_cents": ask, "main_blocker": "price_not_attractive_after_costs",
        "quote": {"bid_cents": 58.0, "ask_cents": 63.0, "spread_cents": 5.0, "ask_depth": 40.0},
        "costs": {"fee_cents": 1.0, "slippage_cents": 0.5, "total_cents": 2.0},
    }


def _cfg(tmp, **kw):
    base = dict(enabled=True, model_version="ir-test",
               db_path=os.path.join(tmp, "ir.sqlite3"), mark_band_seconds=25.0,
               min_data_quality=0.0)
    base.update(kw)
    return IntervalResearchConfig(**base)


class ConfigTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("Q15_INTERVAL_RESEARCH_ENABLED", None)
        cfg_mod.reset_enabled_cache()
        reset_runner()

    def test_eight_marks_and_roles(self):
        self.assertEqual(list(INTERVAL_MARKS), ["15M", "13M", "12M", "11M", "10M", "9M", "8M", "7M"])
        self.assertEqual(INTERVAL_MARKS["10M"], 600)
        self.assertEqual(INTERVAL_ROLES["10M"], "OFFENSIVE_ENTRY")
        self.assertEqual(INTERVAL_ROLES["7M"], "CONFIRMATION_DEFENSIVE")

    def test_default_off(self):
        os.environ.pop("Q15_INTERVAL_RESEARCH_ENABLED", None)
        cfg_mod.reset_enabled_cache()
        reset_runner()
        self.assertFalse(IntervalResearchConfig.from_env().enabled)
        self.assertIsNone(get_runner())


class CaptureResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.r = IntervalResearchRunner(_cfg(self.tmp))

    def _observe(self, ticker, seconds, side="NO", ask=61.0, **kw):
        self.r.observe(analyses={"BTC": _analysis(side=side, ask=ask, **kw)},
                       canonicals={"BTC": _Canon(ticker, seconds)}, now=1000.0)

    def test_capture_in_band_and_dedup(self):
        self._observe("KX1", 600)   # exactly 10M
        self._observe("KX1", 595)   # still in 10M band -> dedup
        rows = self.r.ledger.resolved_rows()  # none resolved yet
        self.assertEqual(rows, [])
        cnt = self.r.ledger.counts()
        self.assertEqual(cnt["by_interval"].get("10M"), 1)
        self.assertEqual(cnt["captured"], 1)

    def test_out_of_band_not_captured(self):
        self._observe("KX2", 650)   # between 11M(660) and 10M(600) bands -> 650 in 11M band [635,660]
        # 650 is within 11M band (660-25=635..660) -> captured as 11M
        self.assertEqual(self.r.ledger.counts()["by_interval"].get("11M"), 1)
        self._observe("KX3", 500)   # 500: 9M band is [515,540]; 8M band [455,480]; 500 in neither
        self.assertIsNone(self.r.ledger.counts()["by_interval"].get("9M"))

    def test_missing_reason_when_model_cannot_score(self):
        self.r.observe(analyses={"BTC": _analysis(available=False)},
                       canonicals={"BTC": _Canon("KXm", 600)}, now=1000.0)
        cnt = self.r.ledger.counts()
        self.assertEqual(cnt["by_missing_reason"].get("MODEL_COULD_NOT_SCORE"), 1)
        self.assertEqual(cnt["captured"], 0)

    def test_resolve_correctness_and_pnl(self):
        self._observe("KXr", 600, side="NO", ask=61.0)
        # settle NO -> correct, pnl = 100 - 61 = 39
        self.r.resolve_settled([{"ticker": "KXr", "result": "NO"}], now=2000.0)
        row = [r for r in self.r.ledger.resolved_rows() if r["ticker"] == "KXr"][0]
        self.assertEqual(row["correct"], 1)
        self.assertAlmostEqual(row["realized_pnl_cents"], 39.0, places=3)

    def test_resolve_loss_pnl(self):
        self._observe("KXl", 600, side="NO", ask=61.0)
        self.r.resolve_settled([{"ticker": "KXl", "result": "YES"}], now=2000.0)
        row = [r for r in self.r.ledger.resolved_rows() if r["ticker"] == "KXl"][0]
        self.assertEqual(row["correct"], 0)
        self.assertAlmostEqual(row["realized_pnl_cents"], -61.0, places=3)

    def test_restart_safe_no_duplicate(self):
        self._observe("KXs", 600)
        # New ledger instance on the SAME path (simulating a restart) + same capture.
        r2 = IntervalResearchRunner(_cfg(self.tmp))
        r2.observe(analyses={"BTC": _analysis()}, canonicals={"BTC": _Canon("KXs", 600)}, now=3000.0)
        self.assertEqual(r2.ledger.counts()["by_interval"].get("10M"), 1)  # still one

    def test_disabled_is_noop(self):
        r = IntervalResearchRunner(_cfg(self.tmp, enabled=False, db_path=os.path.join(self.tmp, "off.sqlite3")))
        r.observe(analyses={"BTC": _analysis()}, canonicals={"BTC": _Canon("KXo", 600)}, now=1000.0)
        self.assertEqual(r.ledger.counts()["captured"], 0)


class EconomicsTest(unittest.TestCase):
    def _row(self, ticker, interval, side, ask, result, recommended=0):
        correct = 1 if side == result else 0
        pnl = (100.0 - ask) if correct else -ask
        return {"ticker": ticker, "interval": interval, "predicted_side": side,
                "entry_ask_cents": ask, "realized_pnl_cents": pnl, "correct": correct,
                "official_result": result, "conservative_probability": 0.7,
                "net_edge_cents": 5.0, "entry_recommended": recommended,
                "mark_seconds": INTERVAL_MARKS[interval], "seconds_remaining": INTERVAL_MARKS[interval]}

    def test_classify_separates_prediction_and_trade(self):
        # 97% accurate but 97c ask -> HIGH prediction / LOW trade value.
        c = econ.classify(0.97, 97.0)
        self.assertEqual(c["prediction_quality"], "HIGH")
        self.assertEqual(c["trade_value"], "LOW")
        # 70% at 61c -> room ~9c -> HIGH trade value.
        c2 = econ.classify(0.70, 61.0)
        self.assertEqual(c2["trade_value"], "HIGH")

    def test_per_interval_economics(self):
        rows = [self._row("A", "10M", "NO", 61.0, "NO"),
                self._row("B", "10M", "NO", 61.0, "YES"),
                self._row("C", "7M", "NO", 97.0, "NO")]
        out = econ.per_interval_economics(rows)
        self.assertEqual(out["10M"]["n"], 2)
        self.assertEqual(out["10M"]["accuracy"], 0.5)
        # 7M bucket captures the rich executable ask (the priced-out case).
        self.assertEqual(out["7M"]["avg_executable_ask_cents"], 97.0)
        self.assertEqual(out["10M"]["avg_executable_ask_cents"], 61.0)

    def test_cohort_split_isolates_late_only(self):
        # late-only contract (7M only) vs a fuller one.
        rows = [self._row("LATE", "7M", "NO", 97.0, "NO")]
        for iv in INTERVAL_MARKS:
            rows.append(self._row("FULL", iv, "NO", 70.0, "NO"))
        split = econ.cohort_split(rows)
        self.assertEqual(split["late_only"]["contracts"], 1)
        self.assertEqual(split["full"]["contracts"], 1)
        self.assertEqual(split["late_only"]["seven_m"]["n"], 1)

    def test_matched_cohort_only_shared_contracts(self):
        rows = [self._row("X", "10M", "NO", 60.0, "NO"), self._row("X", "7M", "NO", 95.0, "NO"),
                self._row("Y", "7M", "NO", 95.0, "NO")]  # Y has no 10M
        m = econ.matched_cohort_comparison(rows, ["10M", "7M"])
        self.assertEqual(m["matched_contracts"], 1)  # only X
        self.assertEqual(m["by_interval"]["7M"]["n"], 1)

    def test_defensive_exit_grade_flip_and_lead(self):
        # Contract flips 10M YES -> 7M NO, settles NO (sustained true warning).
        rows = [self._row("F", "10M", "YES", 55.0, "NO"), self._row("F", "7M", "NO", 96.0, "NO")]
        # fix the 10M correctness/result to the settled NO
        for r in rows:
            r["official_result"] = "NO"
        g = econ.defensive_exit_grade(rows)
        self.assertEqual(g["contracts_with_flip"], 1)
        self.assertEqual(g["true_warnings"], 1)
        self.assertEqual(g["precision"], 1.0)
        # warning at 7M, original side worth ~4c (100-96) -> economically late
        self.assertEqual(g["economically_late"], 1)


if __name__ == "__main__":
    unittest.main()
