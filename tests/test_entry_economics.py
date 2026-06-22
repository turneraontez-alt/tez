"""Entry Economics (entry-econ-v1) — full behavioural test suite.

Covers every scenario the workstream-2 spec enumerates: overpriced winners, an
attractive-looking loser, wide spreads, thin liquidity, manipulation, flip risk,
missing book data, stale prices, a price reaching the recommended range, restart
before settlement, duplicate prevention, and fees/slippage flipping a positive
edge negative. Pure/deterministic — no network, clock injected where needed.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.entry_economics import (
    ENTER, SKIP, WAIT, EntryEconConfig, EntryLedger, compute_economics,
    conservative_probability, estimate_fill, evaluate_analysis, kalshi_fee_cents, render_panel,
)
from q15_upgrade.entry_economics.costs import all_in_costs


def cfg(**over):
    return EntryEconConfig.from_env().with_overrides(**over) if over else EntryEconConfig.from_env()


def base_analysis(**over):
    a = {
        "asset": "SOL", "checkpoint": "10M", "prediction_side": "NO",
        "selected_probability": 0.78, "yes_probability": 0.22,
        "data_quality": 0.9, "evidence_coverage": 0.9, "seconds_remaining": 300,
        "market_implied_yes_probability": 0.30,
        "quote": {"ask_cents": 52, "spread_cents": 2, "depth_contracts": 50,
                  "quote_age_seconds": 2},
    }
    a.update(over)
    return a


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #
class FeeTest(unittest.TestCase):
    def test_kalshi_fee_is_ceiled(self):
        # 0.07*0.63*0.37*100 = 1.6317 -> the exchange rounds UP to 2.
        self.assertEqual(kalshi_fee_cents(63), 2.0)
        self.assertAlmostEqual(kalshi_fee_cents(63, ceil=False), 1.6317, places=3)

    def test_fee_zero_at_extremes(self):
        self.assertEqual(kalshi_fee_cents(0), 0.0)
        self.assertEqual(kalshi_fee_cents(100), 0.0)

    def test_ceil_never_below_unrounded(self):
        for price in range(1, 100):
            self.assertGreaterEqual(kalshi_fee_cents(price) + 1e-9,
                                    kalshi_fee_cents(price, ceil=False))


class FillTest(unittest.TestCase):
    def test_depth_walk_vwap_and_partial_fill(self):
        # 3 @ 52, 2 @ 54, then nothing: filling 5 -> vwap = (3*52+2*54)/5 = 52.8
        levels = [(52, 3), (54, 2)]
        fe = estimate_fill(best_ask_cents=52, ask_levels=levels, sim_contracts=5)
        self.assertTrue(fe.book_known)
        self.assertAlmostEqual(fe.vwap_cents, 52.8, places=2)
        self.assertAlmostEqual(fe.slippage_cents, 0.8, places=2)
        self.assertFalse(fe.partial_fill)
        # Requesting 10 with only 5 available -> partial fill.
        fe2 = estimate_fill(best_ask_cents=52, ask_levels=levels, sim_contracts=10)
        self.assertTrue(fe2.partial_fill)
        self.assertAlmostEqual(fe2.filled_contracts, 5.0, places=4)

    def test_missing_book_uses_fallback_slippage(self):
        fe = estimate_fill(best_ask_cents=52, ask_levels=None, spread_cents=4,
                           slippage_spread_fraction=0.5, sim_contracts=5)
        self.assertFalse(fe.book_known)
        self.assertAlmostEqual(fe.slippage_cents, 2.0, places=2)  # 0.5 * spread

    def test_fees_and_slippage_flip_positive_edge_negative(self):
        # Gross looks positive (fair 55 vs ask 53 = +2) but fee+slippage > 2 -> net < 0.
        from q15_upgrade.entry_economics.costs import CostBreakdown
        fill = estimate_fill(best_ask_cents=53, ask_levels=[(53, 1), (60, 50)], sim_contracts=5)
        costs = all_in_costs(
            ask_cents=53, fill=fill, fee_rate=0.07, fee_ceil=True, latency_cost_cents=0.25,
            quote_age_seconds=2, stale_penalty_cents_per_s=0.05, stale_penalty_cap_cents=3,
            stale_quote_age_seconds=45)
        econ = compute_economics(
            side="NO", model_side_prob=0.55, conservative_side_prob=0.55,
            executable_ask_cents=53, cost_breakdown=costs, fill=fill,
            required_margin_cents=6, recommended_band_cents=3, risk_aversion_k=0.04)
        self.assertGreater(econ.gross_edge_cents, 0.0)       # looked attractive
        self.assertLess(econ.net_edge_cents, 0.0)            # not after real costs


# --------------------------------------------------------------------------- #
# Conservative probability
# --------------------------------------------------------------------------- #
class ConservativeTest(unittest.TestCase):
    def test_shrinks_toward_half_but_not_past(self):
        c = cfg()
        r = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                     checkpoint="10M", resolved_sample_size=200)
        self.assertLess(r.conservative_side_prob, 0.80)
        self.assertGreater(r.conservative_side_prob, 0.50)

    def test_low_coverage_pulls_down_more(self):
        c = cfg()
        hi = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                      data_coverage=0.95, data_quality=0.95,
                                      resolved_sample_size=200)
        lo = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                      data_coverage=0.2, data_quality=0.2,
                                      resolved_sample_size=200)
        self.assertLess(lo.conservative_side_prob, hi.conservative_side_prob)

    def test_flip_and_manip_penalties_apply(self):
        c = cfg()
        clean = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                         resolved_sample_size=200)
        flip = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                        resolved_sample_size=200, flip_score=0.9, flip_confidence=0.9)
        manip = conservative_probability(side="NO", calibrated_side_prob=0.80, cfg=c,
                                         resolved_sample_size=200, manipulation_suspected=True,
                                         manipulation_against_side=True)
        self.assertLess(flip.conservative_side_prob, clean.conservative_side_prob)
        self.assertLess(manip.conservative_side_prob, clean.conservative_side_prob)
        self.assertGreater(flip.flip_penalty, 0.0)
        self.assertGreater(manip.manip_penalty, 0.0)

    def test_missing_inputs_degrade_not_crash(self):
        c = cfg()
        r = conservative_probability(side="YES", calibrated_side_prob=0.70, cfg=c)
        self.assertIn("sample_size_unknown", r.notes)
        self.assertTrue(0.5 < r.conservative_side_prob < 0.70)


# --------------------------------------------------------------------------- #
# Decision scenarios
# --------------------------------------------------------------------------- #
class DecisionScenarioTest(unittest.TestCase):
    def test_strong_pick_good_price_enters(self):
        r = evaluate_analysis(base_analysis(), cfg(), resolved_sample_size=300,
                              calibration_reliability=1.0)
        self.assertEqual(r.decision, ENTER)
        self.assertIsNone(r.main_blocker)
        self.assertGreaterEqual(r.economics.net_edge_cents, cfg().required_edge_for("10M"))

    def test_winning_prediction_but_overpriced_waits(self):
        a = base_analysis(quote={"ask_cents": 70, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, WAIT)
        self.assertEqual(r.main_blocker, "price_too_high")

    def test_attractive_price_but_weak_side_skips(self):
        # A near-coin-flip pick (raw 0.56 -> conservative ~0.54) is below the
        # configured side-validity floor, so even a cheap 40c ask SKIPs: a side we
        # barely believe is not "valid", regardless of how attractive the price looks.
        a = base_analysis(selected_probability=0.56, yes_probability=0.44,
                          quote={"ask_cents": 40, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(min_conservative_side_prob=0.62), resolved_sample_size=300)
        self.assertEqual(r.decision, SKIP)
        self.assertEqual(r.main_blocker, "side_invalid")

    def test_losing_prediction_attractive_price_grades_loss_at_settlement(self):
        # Entry quality != prediction quality: a confident NO at a great price ENTERs,
        # but if the market settles YES the ledger records a loss / wrong direction.
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, ENTER)
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            self.assertTrue(L.mark_enter_sent("SOLX", "10M", model_version=r.version))
            self.assertTrue(L.resolve("SOLX", "10M", "YES", model_version=r.version))
            sb = L.entry_scoreboard(r.version)
            self.assertEqual(sb["official_entries"], 1)
            self.assertEqual(sb["wins"], 0)
            self.assertEqual(sb["direction_accuracy"], 0.0)
            self.assertLess(sb["total_pnl_cents"], 0.0)
            L.close()

    def test_wide_spread_waits(self):
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 20, "depth_contracts": 50,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, WAIT)
        self.assertEqual(r.main_blocker, "spread_too_wide")

    def test_thin_liquidity_waits(self):
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 1,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, WAIT)
        self.assertEqual(r.main_blocker, "insufficient_depth")

    def test_high_manipulation_against_side_waits(self):
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2},
                          manipulation={"suspected": True, "lean": "YES"})  # against our NO
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, WAIT)
        self.assertEqual(r.main_blocker, "manipulation_better_entry")

    def test_high_flip_risk_blocks_enter(self):
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2},
                          flip_risk={"score": 0.85, "confidence": 0.9})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertIn(r.decision, (WAIT, SKIP))
        self.assertEqual(r.main_blocker, "side_unstable")

    def test_missing_orderbook_does_not_skip(self):
        # No depth / no levels: capacity unknown, but the entry is NOT blocked
        # solely for that — it still evaluates on the reliable evidence that remains.
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertNotEqual(r.decision, SKIP)
        self.assertEqual(r.decision, ENTER)  # everything else is fine

    def test_stale_price_skips(self):
        a = base_analysis(quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 90})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, SKIP)
        self.assertEqual(r.main_blocker, "data_stale")

    def test_too_little_time_skips(self):
        a = base_analysis(seconds_remaining=5,
                          quote={"ask_cents": 50, "spread_cents": 2, "depth_contracts": 50,
                                 "quote_age_seconds": 2})
        r = evaluate_analysis(a, cfg(), resolved_sample_size=300)
        self.assertEqual(r.decision, SKIP)
        self.assertEqual(r.main_blocker, "too_little_time")

    def test_no_executable_price_skips_terse(self):
        a = base_analysis(quote={"spread_cents": 2})
        r = evaluate_analysis(a, cfg())
        self.assertEqual(r.decision, SKIP)
        self.assertEqual(r.main_blocker, "no_executable_price")
        self.assertIn("No reliable executable price", render_panel(r))


# --------------------------------------------------------------------------- #
# Panel format
# --------------------------------------------------------------------------- #
class PanelTest(unittest.TestCase):
    def test_panel_has_all_required_lines(self):
        r = evaluate_analysis(base_analysis(quote={"ask_cents": 70, "spread_cents": 2,
                                                   "depth_contracts": 50, "quote_age_seconds": 2}),
                              cfg(), resolved_sample_size=300)
        panel = render_panel(r)
        for needle in ("ENTRY ECONOMICS", "Decision:", "Side: SOL NO", "Current ask:",
                       "Best entry:", "Maximum entry:", "Conservative probability:",
                       "Net edge:", "Main blocker:"):
            self.assertIn(needle, panel)


# --------------------------------------------------------------------------- #
# Ledger: restart, dedup, sent-only counting, excursion, studies
# --------------------------------------------------------------------------- #
class LedgerTest(unittest.TestCase):
    def _enter_result(self):
        return evaluate_analysis(base_analysis(), cfg(), resolved_sample_size=300)

    def test_duplicate_prevention_one_row_per_contract_checkpoint(self):
        r = self._enter_result()
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            id1 = L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            id2 = L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            id3 = L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            self.assertEqual(id1, id2)
            self.assertEqual(id1, id3)
            self.assertEqual(sum(L.decision_counts(r.version).values()), 1)
            L.close()

    def test_wait_and_skip_not_counted_as_trades(self):
        wait = evaluate_analysis(base_analysis(quote={"ask_cents": 70, "spread_cents": 2,
                                                      "depth_contracts": 50, "quote_age_seconds": 2}),
                                 cfg(), resolved_sample_size=300)
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(wait, contract="SOLW", close_time=1000.0)
            # No ENTER sent -> resolve grades direction but never a win/loss.
            L.resolve("SOLW", "10M", "NO", model_version=wait.version)
            sb = L.entry_scoreboard(wait.version)
            self.assertEqual(sb["official_entries"], 0)
            studies = L.background_studies(wait.version)
            self.assertEqual(studies["wait_total_resolved"], 1)
            self.assertEqual(studies["wait_correct_side"], 1)
            L.close()

    def test_only_sent_enter_counts_official(self):
        r = self._enter_result()
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            # Not sent yet -> resolving does not produce an official entry.
            L.resolve("SOLX", "10M", "NO", model_version=r.version)
            self.assertEqual(L.entry_scoreboard(r.version)["official_entries"], 0)

    def test_restart_before_settlement_preserves_sent_enter(self):
        r = self._enter_result()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "e.sqlite3")
            L = EntryLedger(path)
            L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            self.assertTrue(L.mark_enter_sent("SOLX", "10M", model_version=r.version))
            L.close()
            # --- process restart ---
            L2 = EntryLedger(path)
            # Re-recording after restart must not wipe the frozen ENTER.
            L2.record_evaluation(r, contract="SOLX", close_time=1000.0)
            self.assertTrue(L2.resolve("SOLX", "10M", "NO", model_version=r.version))
            sb = L2.entry_scoreboard(r.version)
            self.assertEqual(sb["official_entries"], 1)
            self.assertEqual(sb["wins"], 1)
            self.assertGreater(sb["total_pnl_cents"], 0.0)
            L2.close()

    def test_mark_enter_sent_is_idempotent(self):
        r = self._enter_result()
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            self.assertTrue(L.mark_enter_sent("SOLX", "10M", model_version=r.version, ask_cents=52))
            # Second mark is a no-op (already sent) and must not change the frozen ask.
            self.assertFalse(L.mark_enter_sent("SOLX", "10M", model_version=r.version, ask_cents=99))
            L.close()

    def test_excursion_tracks_favorable_and_adverse(self):
        r = self._enter_result()
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(r, contract="SOLX", close_time=1000.0)
            L.mark_enter_sent("SOLX", "10M", model_version=r.version, ask_cents=52,
                              total_cost_cents=3)
            L.update_excursion("SOLX", "10M", model_version=r.version, current_value_cents=70)  # +15
            L.update_excursion("SOLX", "10M", model_version=r.version, current_value_cents=40)  # -15
            row = L._conn.execute(
                "SELECT max_favorable_cents, max_adverse_cents FROM entry_records").fetchone()
            self.assertAlmostEqual(row["max_favorable_cents"], 15.0, places=2)
            self.assertAlmostEqual(row["max_adverse_cents"], 15.0, places=2)
            L.close()

    def test_mark_enter_sent_refuses_non_enter_row(self):
        wait = evaluate_analysis(base_analysis(quote={"ask_cents": 70, "spread_cents": 2,
                                                      "depth_contracts": 50, "quote_age_seconds": 2}),
                                 cfg(), resolved_sample_size=300)
        self.assertEqual(wait.decision, WAIT)
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(wait, contract="SOLW", close_time=1000.0)
            # A WAIT row can never be credited as a sent official entry.
            self.assertFalse(L.mark_enter_sent("SOLW", "10M", model_version=wait.version))
            self.assertEqual(L.entry_scoreboard(wait.version)["official_entries"], 0)
            L.close()

    def test_runner_ledger_roundtrip_and_settlement(self):
        # Runner with the ledger enabled: evaluate -> persist -> mark sent -> resolve.
        with tempfile.TemporaryDirectory() as d:
            from q15_upgrade.entry_economics.runner import EntryEconRunner
            c = cfg(ledger_enabled=True, db_path=os.path.join(d, "e.sqlite3"))
            runner = EntryEconRunner(c)
            res = runner.evaluate(base_analysis(), contract="SOLX", close_time=1000.0)
            self.assertEqual(res.decision, ENTER)
            self.assertTrue(runner.mark_enter_sent("SOLX", "10M"))
            self.assertTrue(runner.resolve("SOLX", "10M", "NO"))
            sb = runner.scoreboard()
            self.assertEqual(sb["official_entries"], 1)
            self.assertEqual(sb["wins"], 1)
            self.assertEqual(sb["decision_counts"]["ENTER"], 1)

    def test_skip_background_study(self):
        skip = evaluate_analysis(base_analysis(quote={"spread_cents": 2}), cfg())
        with tempfile.TemporaryDirectory() as d:
            L = EntryLedger(os.path.join(d, "e.sqlite3"))
            L.record_evaluation(skip, contract="SOLS", close_time=1000.0)
            L.resolve("SOLS", "10M", "YES", model_version=skip.version)  # side was NO -> avoided a loss
            studies = L.background_studies(skip.version)
            self.assertEqual(studies["skip_avoided_loss"], 1)
            self.assertEqual(studies["skip_missed_win"], 0)
            L.close()


if __name__ == "__main__":
    unittest.main()
