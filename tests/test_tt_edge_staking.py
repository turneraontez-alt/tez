"""Fractional-Kelly staking: fraction math, the 5% cap, the $1 floor, and
their precedence (cap always wins)."""
from decimal import Decimal

import pytest

from tt_edge.edge.staking import StakingError, kelly_fraction, plan_stake


QUARTER = Decimal("0.25")
CAP = Decimal("0.05")


class TestKellyFraction:
    def test_known_value(self):
        # p=0.58 at -120: b=5/6, f* = (b*p - q)/b = 19/250 = 0.076 exactly.
        f = kelly_fraction(Decimal("0.58"), -120)
        assert abs(f - Decimal("0.076")) < Decimal("1e-20")

    def test_even_money(self):
        # p=0.6 at +100: f* = 2p - 1 = 0.2.
        assert kelly_fraction(Decimal("0.6"), 100) == Decimal("0.2")

    def test_negative_ev_clamps_to_zero(self):
        assert kelly_fraction(Decimal("0.5"), -120) == 0
        assert kelly_fraction(Decimal("0.3"), 100) == 0

    @pytest.mark.parametrize("bad", [Decimal(0), Decimal(1), Decimal("1.5")])
    def test_probability_bounds(self, bad):
        with pytest.raises(StakingError):
            kelly_fraction(bad, -120)


class TestPlanStake:
    def test_launch_bankroll_example(self):
        # $65 roll, p=0.58 at -120: 6500 * 0.25 * 0.076 = 123.5 -> 123c.
        plan = plan_stake(6500, Decimal("0.58"), -120, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=100)
        assert plan.stake_cents == 123
        assert not plan.capped and not plan.floored

    def test_cap_binds_at_five_percent(self):
        # Huge edge: raw quarter-Kelly would be 1300c; cap = 325c ($3.25 at
        # $65 — the spec's max-stake example).
        plan = plan_stake(6500, Decimal("0.9"), 100, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=100)
        assert plan.stake_cents == 325
        assert plan.capped

    def test_floor_lifts_dust_stakes(self):
        # p=0.51 at +100: kelly 0.02 -> raw 32c -> floored to $1.
        plan = plan_stake(6500, Decimal("0.51"), 100, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=100)
        assert plan.stake_cents == 100
        assert plan.floored

    def test_cap_dominates_floor(self):
        # $15 roll: cap 75c < floor 100c. Never risk more than the cap.
        plan = plan_stake(1500, Decimal("0.51"), 100, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=100)
        assert plan.stake_cents == 75
        assert plan.floored

    def test_zero_kelly_zero_stake(self):
        plan = plan_stake(6500, Decimal("0.5"), -120, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=100)
        assert plan.stake_cents == 0
        assert not plan.capped and not plan.floored

    def test_rounds_down_to_whole_cents(self):
        # 10000 * 0.25 * 0.076 = 190.0; use 9999 -> 189.981 -> 189.
        plan = plan_stake(9999, Decimal("0.58"), -120, fraction=QUARTER,
                          cap_pct=CAP, floor_cents=1)
        assert plan.stake_cents == 189

    def test_non_positive_bankroll_rejected(self):
        with pytest.raises(StakingError):
            plan_stake(0, Decimal("0.58"), -120, fraction=QUARTER,
                       cap_pct=CAP, floor_cents=100)
