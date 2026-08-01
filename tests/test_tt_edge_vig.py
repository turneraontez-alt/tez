"""De-vig math: American conversions, proportional vig strip, cents scale.

Money math — everything Decimal, exactness asserted tightly.
"""
from decimal import Decimal

import pytest

from tt_edge.edge.vig import (OddsError, american_cents_scale,
                              american_to_decimal_odds, american_to_implied,
                              devig_proportional, movement_cents,
                              probability_to_american, validate_american)


class TestAmericanToImplied:
    def test_negative_favorite(self):
        assert american_to_implied(-120) == Decimal(120) / Decimal(220)

    def test_even_money(self):
        assert american_to_implied(100) == Decimal("0.5")
        assert american_to_implied(-100) == Decimal("0.5")

    def test_positive_underdog(self):
        assert american_to_implied(150) == Decimal(100) / Decimal(250)

    @pytest.mark.parametrize("bad", [0, 99, -99, 50, -1])
    def test_inside_gap_rejected(self, bad):
        with pytest.raises(OddsError):
            american_to_implied(bad)

    def test_non_int_rejected(self):
        with pytest.raises(OddsError):
            validate_american(-120.0)  # type: ignore[arg-type]
        with pytest.raises(OddsError):
            validate_american(True)  # type: ignore[arg-type]


class TestDecimalOdds:
    def test_favorite(self):
        assert american_to_decimal_odds(-120) == \
            Decimal(1) + Decimal(100) / Decimal(120)

    def test_underdog(self):
        assert american_to_decimal_odds(150) == Decimal("2.5")

    def test_even(self):
        assert american_to_decimal_odds(100) == Decimal(2)
        assert american_to_decimal_odds(-100) == Decimal(2)


class TestDevig:
    def test_proportional_strip(self):
        # -120 / +100: implied 6/11 and 1/2, overround 23/22.
        fair_a, fair_b = devig_proportional(american_to_implied(-120),
                                            american_to_implied(100))
        assert abs(fair_a - Decimal(12) / Decimal(23)) < Decimal("1e-25")
        assert abs(fair_b - Decimal(11) / Decimal(23)) < Decimal("1e-25")

    def test_sides_sum_to_one(self):
        fair_a, fair_b = devig_proportional(american_to_implied(-135),
                                            american_to_implied(115))
        assert abs(fair_a + fair_b - 1) < Decimal("1e-25")

    def test_no_vig_market_unchanged(self):
        fair_a, fair_b = devig_proportional(Decimal("0.6"), Decimal("0.4"))
        assert fair_a == Decimal("0.6")
        assert fair_b == Decimal("0.4")

    def test_non_positive_rejected(self):
        with pytest.raises(OddsError):
            devig_proportional(Decimal("0"), Decimal("0.5"))
        with pytest.raises(OddsError):
            devig_proportional(Decimal("0.5"), Decimal("-0.1"))


class TestProbabilityToAmerican:
    def test_spec_example(self):
        # The spec alert: model 58.0% displays as fair -138.
        assert probability_to_american(Decimal("0.58")) == -138

    def test_coin_flip_is_minus_100(self):
        assert probability_to_american(Decimal("0.5")) == -100

    def test_underdog(self):
        assert probability_to_american(Decimal("0.4")) == 150

    def test_round_trip_even(self):
        assert probability_to_american(american_to_implied(-200)) == -200
        assert probability_to_american(american_to_implied(175)) == 175

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(1), Decimal("1.2"),
                                     Decimal("-0.3")])
    def test_out_of_range_rejected(self, bad):
        with pytest.raises(OddsError):
            probability_to_american(bad)


class TestCentsScale:
    def test_scale_positions(self):
        assert american_cents_scale(100) == 0
        assert american_cents_scale(-100) == 0
        assert american_cents_scale(115) == 15
        assert american_cents_scale(-120) == -20

    def test_shortening_is_negative(self):
        # -120 -> -135: backers must risk more; the side shortened.
        assert movement_cents(-120, -135) == -15

    def test_lengthening_is_positive(self):
        assert movement_cents(-120, -105) == 15

    def test_movement_across_even_money_boundary(self):
        # -105 -> +110 passes through the -100/+100 seam continuously.
        assert movement_cents(-105, 110) == 15
        assert movement_cents(110, -105) == -15

    def test_no_movement(self):
        assert movement_cents(-120, -120) == 0
