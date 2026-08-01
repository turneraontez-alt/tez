"""The champion-vs-challenger promotion test must not treat co-settling assets
as independent observations.

``record_prediction`` writes one row per ASSET per checkpoint, so up to 7 rows
share a single 15-minute settlement — one underlying move, not seven draws.
Testing them as independent understates the standard error by roughly
sqrt(design effect), which is how a lucky handful of windows reads as p<0.05.
"""
from __future__ import annotations

from q15_upgrade.ledger_v95 import V95Ledger


def _row(close_time, champion, challenger):
    return {"close_time": close_time, "champion_brier": champion,
            "challenger_brier": challenger}


def _test(rows):
    return V95Ledger._paired_better_test(rows, "champion_brier", "challenger_brier")


def test_co_settling_assets_collapse_to_one_observation():
    """7 assets in one window is ONE cluster, not 7 pairs."""
    rows = [_row(1000.0, 0.30, 0.20) for _ in range(7)]

    result = _test(rows)

    assert result["n"] == 1
    assert result["reason"] == "insufficient_pairs"


def test_independent_windows_each_count_once():
    rows = [_row(1000.0 + i * 900.0, 0.30, 0.20) for i in range(6)]

    result = _test(rows)

    assert result["n"] == 6


def test_duplicated_assets_do_not_inflate_significance():
    """The core failure: the same 6 windows, once with 1 asset and once with 7.
    Replicating each window must not make the evidence look stronger."""
    windows = [(1000.0, 0.30, 0.24), (1900.0, 0.28, 0.30), (2800.0, 0.35, 0.25),
               (3700.0, 0.31, 0.22), (4600.0, 0.26, 0.29), (5500.0, 0.33, 0.21)]
    single = [_row(*w) for w in windows]
    replicated = [_row(*w) for w in windows for _ in range(7)]

    a, b = _test(single), _test(replicated)

    assert a["n"] == b["n"] == 6
    assert a["mean_brier_reduction"] == b["mean_brier_reduction"]
    assert a["p_value"] == b["p_value"], "replication changed the p-value"


def test_within_window_disagreement_is_averaged_not_counted_twice():
    """Assets disagreeing inside one window should partly cancel, not double-count."""
    rows = [_row(1000.0, 0.30, 0.20), _row(1000.0, 0.20, 0.30),
            _row(1900.0, 0.30, 0.20), _row(2800.0, 0.30, 0.20)]

    result = _test(rows)

    assert result["n"] == 3          # three windows
    # First window nets to zero, so the mean over clusters is (0 + .1 + .1)/3.
    assert abs(result["mean_brier_reduction"] - 0.0667) < 0.001


def test_rows_without_a_window_key_keep_prior_behaviour():
    """No close_time (older data, tests) -> each row is its own cluster, as before."""
    rows = [{"champion_brier": 0.30, "challenger_brier": 0.20} for _ in range(5)]

    assert _test(rows)["n"] == 5
