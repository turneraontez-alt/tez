import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.self_review import SelfReviewEngine, ADAPT_RULES
from q15_upgrade.window_focus import FocusSettings


def prediction(side="YES", *, model=0.7, distance=0.7, micro=0.7, flow_skew=0.3,
               groups=3, score=72, consensus=0.85, required_consensus=0.72,
               samples=10, yes_prob=0.7, breaker=False):
    return {
        "side": side,
        "yes_probability": yes_prob,
        "side_probability": yes_prob if side == "YES" else 1.0 - yes_prob,
        "consensus": consensus,
        "required_consensus": required_consensus,
        "sample_count": samples,
        "features": {
            "components": {"model": model, "distance": distance, "microstructure": micro, "chart": 0.6},
            "flow_skew": flow_skew,
            "confirmation_groups": groups,
            "confirmation_score": score,
            "circuit_breaker_active": breaker,
        },
    }


class Store:
    enabled = False


class GradingTests(unittest.TestCase):
    def engine(self, **settings_kwargs):
        return SelfReviewEngine(Store(), FocusSettings(**settings_kwargs))

    def test_correct_wrong_void(self):
        eng = self.engine()
        win = eng.review(ticker="T1", asset="BTC", market_close=None,
                         prediction=prediction("YES"), agreement="SAME",
                         decision_state="AGREEMENT", actual_result="YES")
        self.assertEqual(win["outcome"], "correct")
        loss = eng.review(ticker="T2", asset="BTC", market_close=None,
                          prediction=prediction("YES"), agreement="SAME",
                          decision_state="AGREEMENT", actual_result="NO")
        self.assertEqual(loss["outcome"], "wrong")
        void_market = eng.review(ticker="T3", asset="BTC", market_close=None,
                                 prediction=prediction("YES"), agreement="SAME",
                                 decision_state="AGREEMENT", actual_result="VOID")
        self.assertEqual(void_market["outcome"], "void")
        void_uncertain = eng.review(ticker="T4", asset="BTC", market_close=None,
                                    prediction=prediction("UNCERTAIN"), agreement="CONFLICT",
                                    decision_state="CONFLICT", actual_result="YES")
        self.assertEqual(void_uncertain["outcome"], "void")
        self.assertIn("no_actionable_prediction", void_uncertain["reason_tags"])

    def test_attribution_failure_modes(self):
        eng = self.engine()
        # Strong model + thin sample + low consensus, predicted YES but lost.
        loss = eng.review(ticker="L1", asset="ETH", market_close=None,
                          prediction=prediction("YES", model=0.8, consensus=0.74,
                                                 required_consensus=0.72, samples=5),
                          agreement="SAME", decision_state="AGREEMENT", actual_result="NO")
        self.assertEqual(loss["outcome"], "wrong")
        self.assertIn("model_overconfident", loss["reason_tags"])
        self.assertIn("low_consensus", loss["reason_tags"])
        self.assertIn("thin_evidence", loss["reason_tags"])
        self.assertEqual(loss["primary_reason"], "model_overconfident")

    def test_attribution_flow_and_conflict(self):
        eng = self.engine()
        loss = eng.review(ticker="L2", asset="SOL", market_close=None,
                          prediction=prediction("YES", micro=0.2, flow_skew=-0.4,
                                                 groups=1, score=40),
                          agreement="CONFLICT", decision_state="CONFLICT",
                          actual_result="NO", prior_side="NO")
        self.assertIn("flow_contradicted", loss["reason_tags"])
        self.assertIn("weak_confirmation", loss["reason_tags"])
        self.assertIn("ignored_conflict", loss["reason_tags"])

    def test_attribution_success_patterns(self):
        eng = self.engine()
        win = eng.review(ticker="W1", asset="BTC", market_close=None,
                         prediction=prediction("YES", model=0.8, consensus=0.85,
                                                required_consensus=0.72, groups=3, score=72),
                         agreement="SAME", decision_state="AGREEMENT",
                         actual_result="YES", prior_side="YES")
        self.assertIn("decisive_model", win["reason_tags"])
        self.assertIn("high_consensus", win["reason_tags"])
        self.assertIn("strong_confirmation", win["reason_tags"])
        self.assertIn("clean_agreement", win["reason_tags"])

    def test_pattern_rollup(self):
        eng = self.engine()
        for i in range(3):
            eng.review(ticker=f"R{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8), agreement="SAME",
                       decision_state="AGREEMENT", actual_result="NO")
        status = eng.status()
        loss_pattern = next(p for p in status["patterns"] if p["pattern_key"] == "loss|model_overconfident")
        self.assertEqual(loss_pattern["losses"], 3)
        self.assertEqual(loss_pattern["kind"], "loss")

    def test_void_does_not_count_toward_adapt(self):
        eng = self.engine()
        eng.review(ticker="V1", asset="BTC", market_close=None,
                   prediction=prediction("UNCERTAIN"), agreement="CONFLICT",
                   decision_state="CONFLICT", actual_result="VOID")
        state = eng._adapt["initial_side_probability"]
        self.assertEqual(state["samples_since"], 0)


class AdaptTests(unittest.TestCase):
    def engine(self, **kw):
        defaults = dict(self_review_adapt_min_samples=4, self_review_adapt_loss_rate=0.5,
                        self_review_adapt_eval_window=4, self_review_adapt_decay_window=6)
        defaults.update(kw)
        settings = FocusSettings(**defaults)
        return SelfReviewEngine(Store(), settings), settings

    def _lose(self, eng, n, ticker_prefix="X"):
        for i in range(n):
            eng.review(ticker=f"{ticker_prefix}{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8), agreement="SAME",
                       decision_state="AGREEMENT", actual_result="NO")

    def _win(self, eng, n, ticker_prefix="W"):
        for i in range(n):
            eng.review(ticker=f"{ticker_prefix}{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8, consensus=0.9), agreement="SAME",
                       decision_state="AGREEMENT", actual_result="YES")

    def test_nudge_is_bounded_and_recorded(self):
        eng, settings = self.engine()
        baseline = settings.initial_side_probability
        self._lose(eng, 4)
        self.assertGreater(settings.initial_side_probability, baseline)
        self.assertAlmostEqual(settings.initial_side_probability, baseline + 0.01, places=6)
        state = eng._adapt["initial_side_probability"]
        self.assertTrue(state["in_probation"])
        events = [e for e in eng.status()["recent_adaptation_events"] if e["action"] == "nudge"]
        self.assertTrue(events)

    def test_nudge_never_exceeds_max(self):
        eng, settings = self.engine()
        rule = ADAPT_RULES["model_overconfident"]
        cap = min(rule["hard_max"], FocusSettings().initial_side_probability + rule["span"])
        # Drive many failure cycles; each probation needs eval_window samples to clear.
        for block in range(40):
            self._lose(eng, 4, ticker_prefix=f"n{block}_")
        self.assertLessEqual(settings.initial_side_probability, cap + 1e-9)

    def test_revert_when_nudge_does_not_help(self):
        eng, settings = self.engine()
        baseline = settings.initial_side_probability
        self._lose(eng, 4)  # triggers a nudge, enters probation
        self.assertTrue(eng._adapt["initial_side_probability"]["in_probation"])
        # Keep losing through the eval window -> post rate not better -> revert.
        self._lose(eng, 4, ticker_prefix="r")
        state = eng._adapt["initial_side_probability"]
        self.assertEqual(state["last_action"], "revert")
        self.assertAlmostEqual(settings.initial_side_probability, baseline, places=6)

    def test_circuit_breaker_gates_adaptation(self):
        eng, settings = self.engine()
        baseline = settings.initial_side_probability
        for i in range(8):
            eng.review(ticker=f"B{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8), agreement="SAME",
                       decision_state="AGREEMENT", actual_result="NO", breaker_active=True)
        self.assertEqual(settings.initial_side_probability, baseline)

    def test_decision_time_breaker_gates_adaptation(self):
        eng, settings = self.engine()
        baseline = settings.initial_side_probability
        # Current breaker is clear, but the breaker was active at decision time
        # (frozen into the features) -> adaptation must still be blocked.
        for i in range(8):
            eng.review(ticker=f"D{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8, breaker=True),
                       agreement="SAME", decision_state="AGREEMENT",
                       actual_result="NO", breaker_active=False)
        self.assertEqual(settings.initial_side_probability, baseline)

    def test_auto_adapt_disabled_flag(self):
        eng, settings = self.engine(self_review_auto_adapt=False)
        baseline = settings.initial_side_probability
        self._lose(eng, 8)
        self.assertEqual(settings.initial_side_probability, baseline)

    def test_persisted_adaptation_reapplied_on_restart(self):
        # Simulate a DB-backed store that remembers the adapted value.
        class FakeStore:
            enabled = True
            def __init__(self):
                self.state = {}
            def execute(self, sql, params=None):
                if "q15_threshold_adapt_state" in sql and "INSERT" in sql:
                    self.state[params[0]] = params  # setting -> row
                return True
            def query(self, sql, params=None):
                if "q15_threshold_adapt_state" in sql:
                    return [{"setting": p[0], "value": p[3], "in_probation": p[6],
                             "last_action": p[7], "updated_at": p[8]} for p in self.state.values()]
                return []
        store = FakeStore()
        settings = FocusSettings(self_review_adapt_min_samples=4, self_review_adapt_loss_rate=0.5,
                                 self_review_adapt_eval_window=4)
        eng = SelfReviewEngine(store, settings)
        baseline = settings.initial_side_probability
        for i in range(4):
            eng.review(ticker=f"P{i}", asset="BTC", market_close=None,
                       prediction=prediction("YES", model=0.8), agreement="SAME",
                       decision_state="AGREEMENT", actual_result="NO")
        adapted = settings.initial_side_probability
        self.assertGreater(adapted, baseline)
        # New process: fresh settings + engine sharing the same persisted store.
        settings2 = FocusSettings(self_review_adapt_min_samples=4, self_review_adapt_loss_rate=0.5,
                                  self_review_adapt_eval_window=4)
        SelfReviewEngine(store, settings2)
        self.assertAlmostEqual(settings2.initial_side_probability, adapted, places=6)


if __name__ == "__main__":
    unittest.main()
