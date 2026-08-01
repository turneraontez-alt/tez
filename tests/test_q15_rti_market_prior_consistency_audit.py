from __future__ import annotations

import json

from tools import q15_rti_market_prior_consistency_audit as audit


def _row(*, side="YES", ask=60.0, spread=2.0, opposite=42.0, mid=0.59):
    close_time = audit.FIRST_ELIGIBLE_CLOSE_TIME
    captured_at = close_time - 780.0 + 0.25
    return {
        "id": 1,
        "asset": "BTC",
        "side": side,
        "close_time": close_time,
        "source_captured_at": captured_at,
        "kalshi_microstructure_captured_at": captured_at,
        "entry_ask_cents": ask,
        "spread_cents": spread,
        "threshold_json": json.dumps({
            "rti_opposite_ask_cents": opposite,
            "rti_market_mid_probability": mid,
        }),
    }


def test_yes_and_no_side_midpoints_map_to_same_yes_probability():
    yes = audit.reconstruct_market_yes(_row())
    no = audit.reconstruct_market_yes(_row(
        side="NO", ask=42.0, spread=2.0, opposite=60.0, mid=0.41,
    ))
    assert yes["quote_yes_probability"] == 0.59
    assert yes["stored_yes_probability"] == 0.59
    assert no["quote_yes_probability"] == 0.59
    assert no["stored_yes_probability"] == 0.5900000000000001
    assert yes["absolute_delta"] <= audit.MAX_ABSOLUTE_DELTA
    assert no["absolute_delta"] <= audit.MAX_ABSOLUTE_DELTA


def test_missing_opposite_ask_uses_selected_bid_complement():
    row = _row(opposite=None)
    result = audit.reconstruct_market_yes(row)
    assert result["quote_yes_probability"] == 0.59
    assert result["absolute_delta"] == 0.0


def test_audit_fails_on_reused_or_inconsistent_market_mid():
    report = audit.audit_rows([_row(mid=0.70)])
    assert report["status"] == "FAIL"
    assert report["mismatch_rows"] == 1
    assert report["errors"] == {"market_prior_quote_mismatch": 1}
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False


def test_audit_fails_on_late_or_reused_quote_cutoff():
    late = _row()
    late["source_captured_at"] += 3.0
    late["kalshi_microstructure_captured_at"] += 3.5
    report = audit.audit_rows([late])
    assert report["status"] == "FAIL"
    assert report["errors"] == {
        "market_prior_cutoff_mismatch": 1,
        "market_prior_exact_capture_offset_exceeded": 1,
    }


def test_pre_boundary_rows_cannot_enter_consistency_credit():
    row = _row()
    row["close_time"] = audit.FIRST_ELIGIBLE_CLOSE_TIME - 900.0
    report = audit.audit_rows([row])
    assert report["status"] == "PASS"
    assert report["eligible_rows"] == 0
    assert report["checked_rows"] == 0
