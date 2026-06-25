"""V2 conviction rules (owner-enabled, DEFAULT ON):

Rule A  Q15_ULTOIM_V2_SKIP_12M_UNLESS_MIN — skip 12M delivery unless >=N (default 3)
        assets co-trigger a NO in the same 15-min window THIS cycle; below the
        threshold the picks are downgraded to research (recorded + graded, never
        alerted/fired/executed).
Rule B  Q15_ULTOIM_V2_DOUBLE_10M_ON_MIN — stake 2x on 10M deliveries when >=N (default
        3) assets co-trigger. Leverage on the COUNT, not on correctness: the staked
        paper P&L doubles BOTH wins and losses (a market-wide YES sweep is doubled too).

The trigger count is the number of would-FIRE NO entries among the candidates passed
to a single _decide_interval call — known at decision time, so there is no look-ahead
and it is independent of deliver_top_n.
"""

from __future__ import annotations

import os

import pytest

from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.runner import UltoimV2Runner

MARK = {"15M": 900, "12M": 720, "10M": 600, "7M": 420}


class _Tg:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return {"ok": True, "delivered": True, "muted": False,
                "message_id": len(self.sent), "error": None}


def _runner(tmp_path, **over):
    base = dict(
        enabled=True, model_version="ultoim-v2",
        db_path=str(tmp_path / "v2.sqlite3"), telegram_chat_id="",
        min_confidence=0.55, ask_lo=50.0, ask_hi=72.0, min_edge_cents=2.0,
        mark_band_seconds=90.0, max_spot_stale_seconds=8.0, deliver_top_n=3,
    )
    base.update(over)
    r = UltoimV2Runner(UltoimV2Config(**base))
    r.telegram = _Tg()
    return r


def _cands(n, *, side="NO", sel=0.65, ask=60.0, close=9000.0, tag="w"):
    """n distinct NO candidates that clear the gate (edge = sel*100 - ask)."""
    return [{
        "asset": f"A{i}", "ticker": f"T-{tag}-{i}", "predicted_side": side,
        "selected_probability": sel, "entry_ask_cents": ask,
        "total_cost_cents": 0.0, "close_time": close,
    } for i in range(n)]


def _rows(r):
    return r.ledger._conn.execute(
        "SELECT ticker, interval, fired, stake_multiplier, reason_codes, "
        "hypothetical_pnl_cents FROM ultoim_v2_predictions ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------- #
# config defaults — the owner asked for both ON
# --------------------------------------------------------------------------- #
def test_conviction_defaults_on():
    cfg = UltoimV2Config()
    assert cfg.skip_12m_unless_min is True
    assert cfg.double_10m_on_min is True
    assert cfg.min_triggers_12m == 3
    assert cfg.min_triggers_10m == 3
    assert cfg.double_stake == 2


def test_conviction_flags_reversible_via_env():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("Q15_ULTOIM_V2_SKIP_12M_UNLESS_MIN", "false")
        mp.setenv("Q15_ULTOIM_V2_DOUBLE_10M_ON_MIN", "false")
        mp.setenv("Q15_ULTOIM_V2_DOUBLE_STAKE", "3")
        cfg = UltoimV2Config()
    assert cfg.skip_12m_unless_min is False
    assert cfg.double_10m_on_min is False
    assert cfg.double_stake == 3


# --------------------------------------------------------------------------- #
# Rule B — 10M conviction doubling
# --------------------------------------------------------------------------- #
def test_rule_b_doubles_10m_at_three(tmp_path):
    r = _runner(tmp_path)
    r._decide_interval("10M", MARK["10M"], 1, _cands(3, tag="a"), now=1000.0)
    rows = _rows(r)
    assert len(rows) == 3
    assert all(row["fired"] == 1 and row["stake_multiplier"] == 2 for row in rows)


def test_rule_b_single_stake_under_three(tmp_path):
    r = _runner(tmp_path)
    r._decide_interval("10M", MARK["10M"], 1, _cands(2, tag="b"), now=1000.0)
    rows = _rows(r)
    assert len(rows) == 2
    assert all(row["stake_multiplier"] == 1 for row in rows)


def test_rule_b_off_never_doubles(tmp_path):
    r = _runner(tmp_path, double_10m_on_min=False)
    r._decide_interval("10M", MARK["10M"], 1, _cands(3, tag="c"), now=1000.0)
    assert all(row["stake_multiplier"] == 1 for row in _rows(r))


def test_rule_b_only_10m(tmp_path):
    """7M is never doubled even with >=3 co-triggers (the rule is 10M-only)."""
    r = _runner(tmp_path)
    r._decide_interval("7M", MARK["7M"], 1, _cands(3, tag="d"), now=1000.0)
    assert all(row["stake_multiplier"] == 1 for row in _rows(r))


def test_rule_b_staked_pnl_cuts_both_ways(tmp_path):
    """The doubled stake amplifies BOTH a winning and a losing window — leverage on
    count, not correctness."""
    r = _runner(tmp_path)
    # Window 1: 3 NO @ ask 60 doubled, then settles YES -> every NO loses, 2x.
    r._decide_interval("10M", MARK["10M"], 1, _cands(3, ask=60.0, tag="loss"), now=1000.0)
    for i in range(3):
        r.ledger.resolve("ultoim-v2", f"T-loss-{i}", "YES")
    # Window 2: 3 NO @ ask 60 doubled, then settles NO -> every NO wins, 2x.
    r._decide_interval("10M", MARK["10M"], 2, _cands(3, ask=60.0, tag="win"), now=2000.0)
    for i in range(3):
        r.ledger.resolve("ultoim-v2", f"T-win-{i}", "NO")
    by_ticker = {row["ticker"]: row for row in _rows(r)}
    for i in range(3):
        loss = by_ticker[f"T-loss-{i}"]
        win = by_ticker[f"T-win-{i}"]
        assert loss["stake_multiplier"] == 2 and loss["hypothetical_pnl_cents"] == -120.0
        assert win["stake_multiplier"] == 2 and win["hypothetical_pnl_cents"] == 80.0


def test_rule_b_no_lookahead_count_is_per_cycle(tmp_path):
    """A 2-trigger window decided first stays 1x even though a later 3-trigger window
    doubles — the count is taken from the candidates in THAT call, never retroactively."""
    r = _runner(tmp_path)
    r._decide_interval("10M", MARK["10M"], 1, _cands(2, tag="early"), now=1000.0)
    r._decide_interval("10M", MARK["10M"], 2, _cands(3, tag="late"), now=2000.0)
    by_ticker = {row["ticker"]: row["stake_multiplier"] for row in _rows(r)}
    assert by_ticker["T-early-0"] == 1 and by_ticker["T-early-1"] == 1
    assert all(by_ticker[f"T-late-{i}"] == 2 for i in range(3))


# --------------------------------------------------------------------------- #
# Rule A — 12M skip unless >=3 co-triggers
# --------------------------------------------------------------------------- #
def _runner_12m(tmp_path, **over):
    return _runner(tmp_path, enable_12m=True, deliver_12m=True, **over)


def test_rule_a_skips_lone_12m(tmp_path):
    """Under the threshold: 2 co-triggering 12M NOs do NOT fire; they are recorded as
    research with SKIP_12M_UNDER_MIN, never alerted."""
    r = _runner_12m(tmp_path)
    # ask 70 (>= floor_12m_ask 65) so the 12M floor doesn't block; edge = 75-70 = 5.
    r._decide_interval("12M", MARK["12M"], 1, _cands(2, sel=0.75, ask=70.0, tag="lo"), now=1000.0)
    rows = _rows(r)
    assert rows, "expected research rows to be recorded"
    assert all(row["fired"] == 0 for row in rows)
    assert all("SKIP_12M_UNDER_MIN" in (row["reason_codes"] or "") for row in rows)
    assert r.telegram.sent == []  # nothing alerted


def test_rule_a_delivers_12m_at_three(tmp_path):
    r = _runner_12m(tmp_path)
    r._decide_interval("12M", MARK["12M"], 1, _cands(3, sel=0.75, ask=70.0, tag="hi"), now=1000.0)
    rows = _rows(r)
    assert len(rows) == 3
    assert all(row["fired"] == 1 for row in rows)
    assert all("SKIP_12M_UNDER_MIN" not in (row["reason_codes"] or "") for row in rows)
    # 12M is never stake-doubled (Rule B is 10M-only).
    assert all(row["stake_multiplier"] == 1 for row in rows)
    assert len(r.telegram.sent) == 3


def test_rule_a_off_delivers_lone_12m(tmp_path):
    r = _runner_12m(tmp_path, skip_12m_unless_min=False)
    r._decide_interval("12M", MARK["12M"], 1, _cands(1, sel=0.75, ask=70.0, tag="solo"), now=1000.0)
    rows = _rows(r)
    assert len(rows) == 1 and rows[0]["fired"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
