"""Settlement-edge probe: the arithmetic that decides whether a contract's
outcome is already banked, and the capture that records what the book charges.

Kalshi 15m crypto settles on a 60-sample index average, so the settlement value
is progressively locked in during the final minute. Over 1,245 windows of
existing index data, contracts judged 'locked' at T-45s resolved as computed
100% of the time. What is NOT known is the quoted price at that moment — this
probe collects it. Read-only, default-OFF, no orders.
"""
from __future__ import annotations

import pytest

from tools import settlement_edge_probe as probe


# ------------------------------------------------------------ banked fraction

@pytest.mark.parametrize("window,expected", [
    (0, 0.0), (15, 0.25), (30, 0.5), (45, 0.75), (60, 1.0),
])
def test_banked_fraction(window, expected):
    assert probe.banked_fraction(window) == pytest.approx(expected)


def test_banked_fraction_is_clamped_and_null_safe():
    assert probe.banked_fraction(90) == 1.0
    assert probe.banked_fraction(-5) == 0.0
    assert probe.banked_fraction(None) is None
    assert probe.banked_fraction("junk") is None


# -------------------------------------------------------- frozen settlement

def test_frozen_blends_banked_average_with_current_index():
    # 45 of 60 samples banked at 100.0, index now 200.0 -> 0.75*100 + 0.25*200
    assert probe.frozen_settlement(100.0, 45, 200.0) == pytest.approx(125.0)


def test_frozen_is_the_index_before_the_final_minute_starts():
    assert probe.frozen_settlement(None, 0, 63000.0) == pytest.approx(63000.0)
    assert probe.frozen_settlement(None, None, 63000.0) == pytest.approx(63000.0)


def test_frozen_equals_the_average_once_fully_banked():
    assert probe.frozen_settlement(63010.0, 60, 99999.0) == pytest.approx(63010.0)


# ------------------------------------------------------------------- locking

def test_locked_when_the_remainder_cannot_reach_the_strike():
    # 45/60 banked, so 25% of the average is still open; at 63000 the bound is
    # 0.25 * 63000 * 0.0015 ~= 23.6. A 500-wide gap is unreachable.
    assert probe.is_locked(63500.0, 63000.0, 45, 63000.0) is True


def test_not_locked_when_the_strike_is_within_reach():
    assert probe.is_locked(63005.0, 63000.0, 45, 63000.0) is False


def test_nothing_is_locked_before_the_final_minute():
    """With 0 banked the whole average is still open — never claim certainty."""
    assert probe.is_locked(63200.0, 63000.0, 0, 63000.0) is False


def test_lock_tightens_as_samples_bank():
    """The same gap becomes lockable as more of the average is fixed."""
    gap = 63000.0 + 30.0
    assert probe.is_locked(gap, 63000.0, 15, 63000.0) is False   # 75% still open
    assert probe.is_locked(gap, 63000.0, 55, 63000.0) is True    # 8% still open


def test_a_wide_gap_cannot_lock_on_a_barely_started_window():
    """The move bound is calibrated to a short remainder. With almost nothing
    banked, even a large gap must NOT read as certain — that false positive is
    the one that would stake money on a coin flip."""
    assert probe.is_locked(64000.0, 63000.0, 5, 63000.0) is False
    assert probe.banked_fraction(5) < probe.MIN_BANKED_TO_LOCK


def test_locked_is_false_on_unusable_input():
    assert probe.is_locked(None, 63000.0, 45, 63000.0) is False
    assert probe.is_locked(63500.0, None, 45, 63000.0) is False


# ------------------------------------------------------------ row + storage

def _row(**over):
    base = dict(now=1000.0, asset="BTC", ticker="KXBTC15M-T", close_time=1060.0,
                mark=45, strike=63000.0,
                book={"yes_bid": 88.0, "yes_ask": 90.0, "no_bid": 10.0, "no_ask": 12.0},
                index={"index_px": 63000.0, "final_minute_avg_px": 63500.0,
                       "final_minute_window_size": 45})
    base.update(over)
    return probe.build_row(**base)


def test_build_row_computes_side_and_lock():
    r = _row()
    assert r["banked_fraction"] == pytest.approx(0.75)
    assert r["frozen_settlement"] == pytest.approx(63375.0)
    assert r["implied_side"] == "YES"
    assert r["locked"] == 1
    assert r["yes_ask"] == 90.0


def test_build_row_marks_no_side_below_the_strike():
    r = _row(index={"index_px": 62000.0, "final_minute_avg_px": 62000.0,
                    "final_minute_window_size": 50})
    assert r["implied_side"] == "NO"
    assert r["locked"] == 1


def test_capture_is_idempotent_per_ticker_and_mark(tmp_path):
    con = probe.open_db(str(tmp_path / "p.sqlite3"))
    assert probe.record(con, _row()) is True
    assert probe.record(con, _row()) is False          # same ticker+mark
    assert probe.record(con, _row(mark=30)) is True    # different mark
    con.close()


def test_resolve_then_summary_reports_the_edge(tmp_path):
    con = probe.open_db(str(tmp_path / "p.sqlite3"))
    probe.record(con, _row())
    assert probe.resolve(con, "KXBTC15M-T", "YES") == 1

    s = probe.summary(con)[ "by_mark"][45]

    assert s["locked"] == 1
    assert s["locked_accuracy"] == 1.0
    assert s["mean_ask_cents"] == pytest.approx(90.0)
    # a certainty bought at 90c is 10c of gross edge - the number in question
    assert s["gross_edge_cents"] == pytest.approx(10.0)


def test_probe_is_off_by_default(monkeypatch):
    monkeypatch.delenv("Q15_SETTLE_PROBE", raising=False)
    assert probe.enabled() is False
    monkeypatch.setenv("Q15_SETTLE_PROBE", "true")
    assert probe.enabled() is True


def test_marks_default_and_override(monkeypatch):
    monkeypatch.delenv("Q15_SETTLE_PROBE_MARKS", raising=False)
    assert probe.marks() == probe.DEFAULT_MARKS
    monkeypatch.setenv("Q15_SETTLE_PROBE_MARKS", "30,10,999,junk")
    assert probe.marks() == (30, 10)      # out-of-range and junk dropped
