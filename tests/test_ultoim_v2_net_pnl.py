"""Fee-adjusted P&L on the V2 ledger.

`hypothetical_pnl_cents` is the GROSS binary payoff and ignores transaction
costs, even though the row stores them. Measured over 1,035 fired+settled picks
that overstates the book by +1.36c/bet — larger than its whole apparent edge
(gross +0.61c/bet, net -0.92c/bet). `net_pnl_cents` carries the honest number;
the gross column is preserved so historical rows stay comparable.
"""
from __future__ import annotations

import pytest

from q15_upgrade.ultoim_v2.ledger import (
    _entry_cost_cents,
    kalshi_fee_cents,
)


# ------------------------------------------------------------ the fee curve

@pytest.mark.parametrize("ask,expected", [
    (25.0, 1.3125),    # real fill recorded 0.0131
    (42.0, 1.7052),    # real fill recorded 0.0170
    (49.0, 1.7493),    # real fill recorded 0.0174
])
def test_fee_curve_matches_real_recorded_fills(ask, expected):
    """At realistic order sizes the round-up amortises, so the per-contract fee
    converges on the raw curve — which is what the real fills show."""
    assert kalshi_fee_cents(ask, contracts=200) == pytest.approx(expected, abs=0.02)


def test_fee_is_quadratic_not_flat():
    """The whole point: a flat fee is ~3x too harsh at the tails."""
    mid = kalshi_fee_cents(50.0, contracts=200)
    tail = kalshi_fee_cents(97.0, contracts=200)

    assert mid == pytest.approx(1.75, abs=0.02)
    assert tail == pytest.approx(0.204, abs=0.02)
    assert mid > tail * 5


def test_round_up_dominates_on_a_single_contract():
    """Kalshi rounds the ORDER fee up to the cent, so a lone deep-favourite
    contract pays ~1c, not the 0.2c the raw curve implies. Ignoring this
    overstates any tail-priced edge."""
    assert kalshi_fee_cents(97.0, contracts=1) == pytest.approx(1.0, abs=0.001)
    assert kalshi_fee_cents(97.0, contracts=200) < 0.3


def test_fee_is_symmetric_and_bounded():
    assert kalshi_fee_cents(30.0, contracts=200) == pytest.approx(
        kalshi_fee_cents(70.0, contracts=200), abs=0.02)
    for ask in (0.0, 1.0, 50.0, 99.0, 100.0):
        assert 0.0 <= kalshi_fee_cents(ask, contracts=200) <= 1.78


# --------------------------------------------------------- cost resolution

def test_recorded_total_cost_wins():
    assert _entry_cost_cents(2.5, 1.7, 55.0) == 2.5


def test_falls_back_to_fee_then_to_the_curve():
    assert _entry_cost_cents(None, 1.7, 55.0) == 1.7
    # No recorded cost at all -> charge the real curve, never zero. The fallback
    # prices a SINGLE contract, so the exchange's round-up applies (2c, not
    # 1.75c) — deliberately the conservative direction for a legacy row.
    assert _entry_cost_cents(None, None, 50.0) == pytest.approx(2.0, abs=0.01)


def test_unusable_values_do_not_crash_or_zero_the_cost():
    assert _entry_cost_cents("junk", None, 50.0) == pytest.approx(2.0, abs=0.01)
    assert _entry_cost_cents(None, None, None) == 0.0


# ------------------------------------------------- net vs gross on settle

def _settle(tmp_path, *, ask, cost, side, official):
    from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger

    from tests.test_ultoim_v2 import _row

    tmp_path.mkdir(parents=True, exist_ok=True)
    led = UltoimV2Ledger(str(tmp_path / "v2.sqlite3"))
    row = dict(_row(ticker="T-BTC", predicted_side=side, entry_ask_cents=ask))
    row["total_cost_cents"] = cost
    assert led.record_decision(row) is not None, "record_decision did not insert"
    assert led.resolve("ultoim-v2", "T-BTC", official, 9500.0) == 1
    con = led._conn
    return con.execute(
        "SELECT hypothetical_pnl_cents, net_pnl_cents, correct "
        "FROM ultoim_v2_predictions WHERE ticker='T-BTC'").fetchone()


def test_win_nets_the_cost_out_of_the_gross_payoff(tmp_path):
    row = _settle(tmp_path, ask=70.0, cost=2.0, side="NO", official="NO")

    assert row["correct"] == 1
    assert row["hypothetical_pnl_cents"] == pytest.approx(30.0)   # gross, unchanged
    assert row["net_pnl_cents"] == pytest.approx(28.0)            # 100-70-2


def test_loss_adds_the_cost_to_the_gross_loss(tmp_path):
    row = _settle(tmp_path, ask=70.0, cost=2.0, side="NO", official="YES")

    assert row["correct"] == 0
    assert row["hypothetical_pnl_cents"] == pytest.approx(-70.0)
    assert row["net_pnl_cents"] == pytest.approx(-72.0)


def test_net_is_always_worse_than_gross(tmp_path):
    """There is no configuration in which ignoring costs is conservative."""
    for ask, official in ((55.0, "NO"), (55.0, "YES"), (90.0, "NO"), (90.0, "YES")):
        row = _settle(tmp_path / str(ask) / official, ask=ask, cost=1.5,
                      side="NO", official=official)
        assert row["net_pnl_cents"] < row["hypothetical_pnl_cents"]
