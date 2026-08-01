from __future__ import annotations

import json
import sqlite3

import pytest

from tools.q15_rti_forward_drift_audit import (
    _fisher_two_sided,
    _valid_exact_row,
    audit,
)
from tools.q15_rti_improvement_audit import _net_pnl_per_contract


def _source(*, close_time=1800.0, correct=1, ask=60.0, **profile_overrides):
    decision_time = close_time - 780.0
    profile = {
        "passed": True,
        "capture_mode": "kalshi_ws_exact_13m",
        "quote_captured_at": decision_time + 0.1,
        "rti_timing_offset_s": 0.1,
        "rti_path_evaluation_delay_s": 0.3,
        "quote_age_seconds": 0.2,
        "rti_path_complete": True,
        "rti_path_count": 61,
        "rti_path_expected_count": 61,
        "rti_signed_distance_bps": 2.0,
        "rti_side_move_bps": 0.5,
        "rti_path_acceleration_bps": 0.1,
        "rti_path_second_half_side_move_bps": 0.2,
        "spot_depth_imbalance": 0.1,
    }
    profile.update(profile_overrides)
    return {
        "id": 1,
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "threshold_json": json.dumps(profile),
        "close_time": close_time,
        "asset": "BTC",
        "side": "YES",
        "official_result": "YES" if correct else "NO",
        "correct": correct,
        "entry_ask_cents": ask,
        "spread_cents": 1.0,
        "hypothetical_pnl_cents": float(
            _net_pnl_per_contract(ask, bool(correct))
        ),
    }


def test_exact_row_integrity_rejects_timestamp_and_economics_mismatch():
    valid, reason = _valid_exact_row(_source())
    assert reason is None
    assert valid is not None

    stale, reason = _valid_exact_row(_source(rti_timing_offset_s=2.1))
    assert stale is None
    assert reason == "capture_offset_incoherent"

    wrong_economics = _source()
    wrong_economics["hypothetical_pnl_cents"] = 999.0
    invalid, reason = _valid_exact_row(wrong_economics)
    assert invalid is None
    assert reason == "economics_incoherent"


def test_fisher_exact_is_symmetric_and_bounded():
    assert _fisher_two_sided(7, 3, 25, 33) == pytest.approx(
        _fisher_two_sided(25, 33, 7, 3)
    )
    assert 0.0 <= _fisher_two_sided(7, 3, 25, 33) <= 1.0


def test_forward_audit_keeps_frozen_and_seen_forward_periods_separate(tmp_path):
    db = tmp_path / "strategy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE strategy_bot_decisions ("
        "id INTEGER PRIMARY KEY, bot_name TEXT, source_system TEXT, "
        "interval TEXT, record_kind TEXT, threshold_json TEXT, close_time REAL, "
        "asset TEXT, side TEXT, official_result TEXT, correct INTEGER, "
        "entry_ask_cents REAL, spread_cents REAL, hypothetical_pnl_cents REAL)"
    )
    sources = [
        _source(close_time=1800.0, correct=1),
        _source(close_time=1800.0, correct=0),
        _source(close_time=2700.0, correct=1),
        _source(close_time=2700.0, correct=0),
    ]
    for row_id, source in enumerate(sources, 1):
        conn.execute(
            "INSERT INTO strategy_bot_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id, "rti_path_13m", "rti_path_13m", "13M",
                source["record_kind"], source["threshold_json"],
                source["close_time"], source["asset"], source["side"],
                source["official_result"], source["correct"],
                source["entry_ask_cents"], source["spread_cents"],
                source["hypothetical_pnl_cents"],
            ),
        )
    conn.commit()
    conn.close()

    report = audit(strategy_db=str(db), freeze_close_time=1800.0)
    assert report["integrity"]["valid_exact_rows"] == 4
    assert report["integrity"]["rejected_rows"] == 0
    assert report["periods"]["frozen_historical"]["overall"]["n"] == 2
    assert report["periods"]["post_freeze_forward"]["overall"]["n"] == 2
    assert report["accuracy_change_test"]["frozen_correct"] == 1
    assert report["accuracy_change_test"]["forward_correct"] == 1
    assert report["interpretation_guardrails"][
        "post_freeze_period_is_no_longer_untouched_for_new_rules"
    ] is True
