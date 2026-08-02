from __future__ import annotations

import json
import sqlite3

from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as identity
from tools import q15_rti_v19_readiness as readiness


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _parent(asset: str, row_id: int) -> dict:
    close = identity.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.2
    return {
        "id": row_id,
        "ticker": f"KX{asset}15M-V19",
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "interval": "13M",
        "asset": asset,
        "close_time": close,
        "side": "YES",
        "entry_ask_cents": 57.0,
        "spread_cents": 1.0,
        "source_captured_at": captured,
        "evidence_as_of": captured + 0.05,
        "threshold_json": {
            "asset_cohort": asset,
            "rti_side": "YES",
            "paper_only": True,
            "passed": asset == "ETH",
            "rule_version": "test-exact-v3",
            "rti_risk_policy_version": v18_identity.RISK_POLICY_VERSION,
            "rti_reversal_risk_class": "low" if asset == "ETH" else "medium",
            "rti_reversal_risk_reason_codes": [],
            "rti_path_status": "ok",
            "rti_path_complete": True,
            "rti_path_expected_count": 61,
            "rti_path_count": 61,
            "rti_path_max_receive_age_s": 0.1,
            "rti_decision_age_s": 0.2,
            "rti_timing_offset_s": 0.2,
            "rti_path_evaluation_delay_s": 0.05,
            "quote_age_seconds": 0.1,
            "quote_age_source": "kalshi_ws_exact_sampler",
            "quote_evidence_source": "kalshi_official_websocket_book",
        },
    }


def _delayed(parent: dict, row_id: int) -> dict:
    target = float(parent["close_time"]) - 720.0
    captured = target + 0.2
    return {
        "id": row_id,
        "ticker": parent["ticker"],
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
        "interval": "12M",
        "asset": parent["asset"],
        "close_time": parent["close_time"],
        "side": "YES",
        "paper_only": True,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "threshold_json": {
            "paper_only": True,
            "rti_confirm_original_row_id": parent["id"],
            "rti_confirm_original_strict_accepted": parent["asset"] == "ETH",
            "rti_confirm_original_side": "YES",
            "rti_confirm_side": "YES",
            "rti_confirm_target_at": target,
            "rti_confirm_delay_seconds": 60.0,
            "rti_confirm_quote_captured_at": captured,
            "rti_confirm_evaluated_at": captured + 0.05,
            "rti_confirm_timing_offset_s": 0.2,
            "rti_confirm_evaluation_delay_s": 0.25,
            "rti_confirm_path_complete": True,
            "rti_confirm_path_expected_count": 61,
            "rti_confirm_path_count": 61,
            "rti_confirm_path_max_receive_age_s": 0.1,
            "rti_confirm_path_decision_age_s": 0.2,
            "quote_age_seconds": 0.1,
            "quote_age_source": "kalshi_ws_exact_sampler",
            "quote_evidence_source": "kalshi_official_websocket_book",
            "sim_contracts": 10,
            "sim_full_fill_supported": True,
        },
    }


def test_readiness_counts_only_all_seven_lineage_complete_windows(monkeypatch):
    parents = [_parent(asset, index + 1) for index, asset in enumerate(ASSETS)]
    delayed = [_delayed(parent, 101 + index) for index, parent in enumerate(parents)]
    monkeypatch.setattr(
        readiness, "_complete_windows",
        lambda _rows: {identity.FIRST_ELIGIBLE_CLOSE_TIME: parents},
    )
    result = readiness.build_readiness(parents, delayed)
    assert result["matched_parent_complete_close_windows"] == 1, json.dumps(
        result["source_health"], sort_keys=True
    )
    assert result["eligible_picks"] == 1
    assert result["candidate_picks_by_asset"] == {"ETH": 1}
    assert result["outcome_columns_selected"] is False
    assert result["outcome_labels_read"] is False
    assert result["notification_eligible"] is False

    missing = readiness.build_readiness(parents, delayed[:-1])
    assert missing["matched_parent_complete_close_windows"] == 0
    assert missing["eligible_picks"] == 0
    assert missing["source_health"]["missing_delayed_rows"] == 1


def test_delayed_loader_cannot_return_outcome_columns(tmp_path):
    db = tmp_path / "strategy.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE strategy_bot_decisions ("
        "id INTEGER,bot_name TEXT,record_kind TEXT,interval TEXT,ticker TEXT,"
        "asset TEXT,side TEXT,close_time REAL,paper_only INTEGER,"
        "entry_ask_cents REAL,spread_cents REAL,depth_contracts REAL,"
        "threshold_json TEXT,official_result TEXT,correct INTEGER,"
        "hypothetical_pnl_cents REAL,resolved_at REAL)"
    )
    parent = _parent("ETH", 1)
    delayed = _delayed(parent, 2)
    connection.execute(
        "INSERT INTO strategy_bot_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            delayed["id"], delayed["bot_name"], delayed["record_kind"],
            delayed["interval"], delayed["ticker"], delayed["asset"],
            delayed["side"], delayed["close_time"], 1,
            delayed["entry_ask_cents"], delayed["spread_cents"],
            delayed["depth_contracts"], json.dumps(delayed["threshold_json"]),
            "YES", 1, 10.0, delayed["close_time"] + 1.0,
        ),
    )
    connection.commit()
    connection.close()
    rows = readiness.load_delayed_feature_rows_after(
        db, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    assert len(rows) == 1
    assert "official_result" not in rows[0]
    assert "correct" not in rows[0]
    assert "hypothetical_pnl_cents" not in rows[0]
    assert "resolved_at" not in rows[0]
