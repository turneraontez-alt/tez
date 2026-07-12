from __future__ import annotations

import json
import sqlite3

from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
from q15_upgrade.strategy_bots.rules import ACCEPTED, STRATEGY_VERSION, BotDecision


def _decision(**threshold_overrides) -> BotDecision:
    threshold = {
        "build_sha": "decision-sha",
        "config_hash": "decision-config",
        "feature_schema_version": "drift-features-v7",
        "feature_cohort": "full-feature-matched",
        "evidence_grade": "A",
        "evidence_reason_codes": ["V91_PATH_YES", "V95_15M_AGREES"],
        "drift_candidate_lane": "ASYMMETRIC_65_73",
        "full_feature_complete": True,
        "evidence_as_of": 998.5,
        "drift_v95_15m_side": "YES",
        "drift_v95_15m_flow_score": 0.25,
        "drift_btc_15m_side": "YES",
    }
    threshold.update(threshold_overrides)
    return BotDecision(
        "drift_flow_spread_13m",
        ACCEPTED,
        ("TEST_DRIFT_LINEAGE",),
        threshold_profile=threshold,
        side_override="YES",
        entry_ask_cents=67.0,
        use_entry_ask_override=True,
    )


def _row(**overrides):
    row = {
        "created_at": 1000.0,
        "captured_at": 999.0,
        "model_version": "interval-research-v1",
        "features_version": "drift-shadow-v7",
        "source_build_sha": "source-sha",
        "source_config_hash": "source-config",
        "record_kind": "DRIFT_PICK_13M",
        "asset": "SOL",
        "ticker": "KXSOL15M-LINEAGE",
        "interval": "13M",
        "window_key": 123,
        "close_time": 1900.0,
        "predicted_side": "YES",
        "entry_ask_cents": 67.0,
        "spread_cents": 1.0,
        "feature_availability": {"v91": True, "v95_15m": True, "btc": True},
        "feature_age_json": '{"btc_15m":118.0,"v95_15m":120.0}',
        "data_complete": False,
        "drift_evidence": {
            "drift_v91_yes_fraction_all": 0.875,
            "drift_v91_yes_fraction_directional": 1.0,
            "drift_v91_observation_count": 8,
            "drift_core_breadth": 1,
            "drift_asymmetric_breadth": 3,
            "drift_flow_1m": 125.0,
            "drift_flow_3m": 300.0,
            "drift_flow_5m": 525.0,
            "drift_flow_13m": 900.0,
            "drift_flow_positive_bucket_fraction": 0.75,
            "drift_flow_sign_flips": 2,
            "drift_flow_coverage": 1.0,
        },
    }
    row.update(overrides)
    return row


def test_drift_lineage_and_evidence_are_first_class_and_lossless(tmp_path):
    ledger = StrategyBotLedger(tmp_path / "lineage.sqlite3")
    row_id = ledger.record_decision(
        _decision(), _row(), source_system="drift_shadow"
    )
    assert row_id is not None
    stored = ledger.row_by_id(row_id)
    assert stored is not None

    assert stored["build_sha"] == "decision-sha"
    assert stored["config_hash"] == "decision-config"
    assert stored["source_build_sha"] == "source-sha"
    assert stored["source_config_hash"] == "source-config"
    assert stored["feature_schema_version"] == "drift-features-v7"
    assert stored["source_features_version"] == "drift-shadow-v7"
    assert stored["feature_cohort"] == "full-feature-matched"
    assert stored["evidence_grade"] == "A"
    assert stored["evidence_reason_codes"] == "V91_PATH_YES,V95_15M_AGREES"
    assert stored["drift_candidate_lane"] == "ASYMMETRIC_65_73"
    assert stored["data_complete"] == 0
    assert stored["full_feature_complete"] == 1
    assert stored["source_created_at"] == 1000.0
    assert stored["source_captured_at"] == 999.0
    assert stored["evidence_as_of"] == 998.5

    assert json.loads(stored["feature_availability_json"]) == {
        "btc": True,
        "v91": True,
        "v95_15m": True,
    }
    assert json.loads(stored["feature_age_json"]) == {
        "btc_15m": 118.0,
        "v95_15m": 120.0,
    }
    assert json.loads(stored["drift_evidence_json"])["drift_flow_13m"] == 900.0

    assert stored["drift_v91_yes_fraction_all"] == 0.875
    assert stored["drift_v91_yes_fraction_directional"] == 1.0
    assert stored["drift_v91_observation_count"] == 8
    assert stored["drift_v95_15m_side"] == "YES"
    assert stored["drift_v95_15m_flow_score"] == 0.25
    assert stored["drift_btc_15m_side"] == "YES"
    assert stored["drift_core_breadth"] == 1
    assert stored["drift_asymmetric_breadth"] == 3
    assert stored["drift_flow_1m"] == 125.0
    assert stored["drift_flow_3m"] == 300.0
    assert stored["drift_flow_5m"] == 525.0
    assert stored["drift_flow_13m"] == 900.0
    assert stored["drift_flow_positive_bucket_fraction"] == 0.75
    assert stored["drift_flow_sign_flips"] == 2
    assert stored["drift_flow_coverage"] == 1.0

    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)
    assert scoreboard["by_drift_candidate_lane"]["ASYMMETRIC_65_73"]["rows"] == 1
    assert scoreboard["by_feature_cohort"]["full-feature-matched"]["rows"] == 1
    assert scoreboard["by_evidence_grade"]["A"]["rows"] == 1
    assert scoreboard["by_full_feature_complete"]["1"]["rows"] == 1
    ledger.close()


def test_generic_rows_leave_unknown_evidence_null_without_imputation(tmp_path):
    ledger = StrategyBotLedger(tmp_path / "generic.sqlite3")
    decision = BotDecision("baseline_control", ACCEPTED, ("CONTROL",))
    row_id = ledger.record_decision(
        decision,
        {
            "created_at": 100.0,
            "model_version": "generic-v1",
            "record_kind": "CONTROL",
            "asset": "DOGE",
            "ticker": "KXDOGE-GENERIC",
            "interval": "10M",
            "window_key": 1,
            "predicted_side": "YES",
            "entry_ask_cents": 60.0,
        },
        source_system="generic",
    )
    stored = ledger.row_by_id(row_id)
    assert stored is not None
    assert stored["source_created_at"] == 100.0
    for key in (
        "build_sha",
        "config_hash",
        "source_build_sha",
        "source_config_hash",
        "feature_schema_version",
        "feature_availability_json",
        "evidence_grade",
        "drift_candidate_lane",
        "data_complete",
        "full_feature_complete",
        "source_captured_at",
        "drift_v91_yes_fraction_all",
        "drift_v95_15m_side",
        "drift_flow_13m",
    ):
        assert stored[key] is None
    ledger.close()


def test_existing_database_additively_migrates_lineage_columns(tmp_path):
    db = tmp_path / "migrate.sqlite3"
    ledger = StrategyBotLedger(db)
    ledger.close()

    removed = (
        "build_sha",
        "source_build_sha",
        "full_feature_complete",
        "drift_v95_15m_side",
        "drift_flow_13m",
    )
    with sqlite3.connect(db) as connection:
        for column in removed:
            connection.execute(
                f"ALTER TABLE strategy_bot_decisions DROP COLUMN {column}"
            )

    migrated = StrategyBotLedger(db)
    with sqlite3.connect(db) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(strategy_bot_decisions)")
        }
    assert set(removed).issubset(columns)
    row_id = migrated.record_decision(
        _decision(), _row(ticker="KXSOL15M-MIGRATED"), source_system="drift_shadow"
    )
    assert row_id is not None
    assert migrated.row_by_id(row_id)["drift_flow_13m"] == 900.0
    migrated.close()
