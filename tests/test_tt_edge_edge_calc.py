"""The single-source edge calculation and every abstain condition."""
from decimal import Decimal

import pytest

from tests.tt_edge_fixtures import make_config
from tt_edge.edge.edge_calc import (NO_BET, RECOMMEND, MarketView,
                                    compute_edge, evaluate)
from tt_edge.edge.vig import american_to_implied, devig_proportional
from tt_edge.freshness import FreshnessCheck

CFG = make_config()
FRESH = FreshnessCheck("board", 240.0, 1800, True)
STALE = FreshnessCheck("board", 1900.0, 1800, False)

# -120 / +100: fair home probability is exactly 12/23 ~ 0.5217.
FAIR_HOME, FAIR_AWAY = devig_proportional(american_to_implied(-120),
                                          american_to_implied(100))


def _eval(**overrides):
    kwargs = dict(model_p_home=Decimal("0.58"),
                  market=MarketView(home_price=-120, away_price=100),
                  freshness_checks=[FRESH], h2h_meetings=10,
                  common_opponents=4, open_recommendations=0,
                  bankroll_cents=6500, config=CFG)
    kwargs.update(overrides)
    return evaluate(**kwargs)


class TestComputeEdge:
    def test_is_model_minus_fair(self):
        assert compute_edge(Decimal("0.58"), Decimal("0.52")) == Decimal("0.06")
        assert compute_edge(Decimal("0.50"), Decimal("0.52")) == Decimal("-0.02")


class TestRecommendPath:
    def test_home_recommendation(self):
        decision = _eval()
        assert decision.status == RECOMMEND
        assert decision.side == "home"
        assert decision.fair_p == FAIR_HOME
        assert decision.edge == Decimal("0.58") - FAIR_HOME
        assert decision.stake.stake_cents == 123      # quarter-Kelly at $65
        assert decision.reasons == ()

    def test_away_pick_uses_away_price(self):
        decision = _eval(model_p_home=Decimal("0.40"))
        assert decision.status == RECOMMEND
        assert decision.side == "away"
        assert decision.model_p == Decimal("0.60")
        assert decision.price_american == 100
        assert decision.fair_p == FAIR_AWAY

    def test_edge_exactly_at_threshold_recommends(self):
        decision = _eval(model_p_home=FAIR_HOME + Decimal("0.05"))
        assert decision.status == RECOMMEND
        assert decision.edge == Decimal("0.05")


class TestAbstains:
    def test_stale_data_hard_pass(self):
        decision = _eval(freshness_checks=[FRESH, STALE])
        assert decision.status == NO_BET
        assert "stale_board" in decision.reasons
        assert decision.edge is not None          # numbers still logged

    def test_insufficient_data(self):
        decision = _eval(h2h_meetings=4, common_opponents=2)
        assert "insufficient_data" in decision.reasons

    def test_enough_common_opponents_compensates_thin_h2h(self):
        decision = _eval(h2h_meetings=4, common_opponents=3)
        assert "insufficient_data" not in decision.reasons

    def test_enough_h2h_compensates_no_common(self):
        decision = _eval(h2h_meetings=5, common_opponents=0)
        assert "insufficient_data" not in decision.reasons

    def test_fix_risk_movement_blocks_even_a_big_edge(self):
        decision = _eval(model_p_home=Decimal("0.75"),
                         market=MarketView(-120, 100,
                                           favored_side_movement_cents=15))
        assert decision.status == NO_BET
        assert "fix_risk_line_move" in decision.reasons

    def test_movement_below_fix_risk_passes(self):
        decision = _eval(market=MarketView(-120, 100,
                                           favored_side_movement_cents=14))
        assert decision.status == RECOMMEND

    def test_adverse_movement_toward_pick_is_fine(self):
        decision = _eval(market=MarketView(-120, 100,
                                           favored_side_movement_cents=-25))
        assert decision.status == RECOMMEND

    def test_no_edge_band(self):
        decision = _eval(model_p_home=Decimal("0.53"))
        assert decision.reasons == ("no_edge",)

    def test_edge_below_threshold(self):
        decision = _eval(model_p_home=Decimal("0.56"))
        assert decision.reasons == ("edge_below_threshold",)

    def test_negative_edge_is_no_bet(self):
        # Model likes home a lot less than the market: edge on the picked
        # (home) side is negative and large -> not "no edge", just no bet.
        decision = _eval(model_p_home=Decimal("0.51"))
        assert decision.status == NO_BET

    def test_no_bankroll(self):
        assert _eval(bankroll_cents=None).reasons == ("no_bankroll_set",)
        assert _eval(bankroll_cents=0).reasons == ("no_bankroll_set",)

    def test_max_concurrent_open(self):
        decision = _eval(open_recommendations=3)
        assert decision.reasons == ("max_concurrent_open",)
        assert _eval(open_recommendations=2).status == RECOMMEND

    def test_all_failed_guards_logged_together(self):
        decision = _eval(freshness_checks=[STALE], h2h_meetings=0,
                         common_opponents=0, model_p_home=Decimal("0.53"))
        assert set(decision.reasons) == {"stale_board", "insufficient_data",
                                         "no_edge"}

    def test_exposure_guards_do_not_mask_pick_guards(self):
        # A stale pick with no bankroll reports the stale reason, not the
        # bankroll one (exposure is only checked for qualifying picks).
        decision = _eval(freshness_checks=[STALE], bankroll_cents=None)
        assert decision.reasons == ("stale_board",)


class TestValidation:
    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(1), Decimal("-0.2")])
    def test_model_probability_bounds(self, bad):
        with pytest.raises(ValueError):
            _eval(model_p_home=bad)
