from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from q15_upgrade.rti_spot_rest_top_book import (
    DEPTH_SCOPE,
    EVIDENCE_COLUMNS,
    STAGE_DELAY_SECONDS,
)
from q15_upgrade.strategy_bots import rti_microstructure_v21 as v21_source
from q15_upgrade.strategy_bots import rti_microstructure_v22 as v22
from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as features
from q15_upgrade.strategy_bots import rti_spot_rest_top_book_reservoir_identity as rest_identity
from tests.test_q15_rti_v21_trajectory import _parent, _stage


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _triplet():
    parent = _parent(
        close_time=identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME,
    )
    return parent, _stage(parent, 30, 201), _stage(parent, 60, 202)


def _rest_rows(parent=None):
    parent = _triplet()[0] if parent is None else parent
    output = []
    close = float(parent["close_time"])
    asset = str(parent["asset"])
    provider, symbol, quote = rest_identity.SOURCE_IDENTITIES[asset]
    for index, stage in enumerate(features.STAGES):
        target = close - 780.0 + STAGE_DELAY_SECONDS[stage]
        bid = 100.0 + index * 0.02
        ask = bid + 0.02
        bid_size = 12.0 + index
        ask_size = 10.0 - index * 0.25
        mid = (bid + ask) / 2.0
        received = target + 0.3
        evidence = {
            "protocol_id": rest_identity.PROTOCOL_ID,
            "protocol_sha256": rest_identity.PROTOCOL_SHA256,
            "schema_version": rest_identity.SCHEMA_VERSION,
            "submitted_at": target + 0.05,
            "request_started_at": target + 0.1,
            "received_at": received,
            "target_at": target,
            "request_start_offset_seconds": 0.1,
            "response_latency_seconds": 0.2,
            "receive_offset_seconds": 0.3,
            "asset": asset,
            "ticker": parent["ticker"],
            "close_time": close,
            "stage": stage,
            "provider": provider,
            "symbol": symbol,
            "quote_currency": quote,
            "depth_scope": DEPTH_SCOPE,
            "status": "OK",
            "failure_reason": None,
            "http_status": 200,
            "source_timestamp": received - 0.1,
            "source_mutation_age_seconds": 0.1,
            "source_sequence": str(index + 1),
            "best_bid": bid,
            "bid_size": bid_size,
            "best_ask": ask,
            "ask_size": ask_size,
            "mid": mid,
            "spread_bps": (ask - bid) / mid * 10_000.0,
            "top_imbalance": (
                (bid_size - ask_size) / (bid_size + ask_size)
            ),
        }
        assert tuple(evidence) == EVIDENCE_COLUMNS
        raw = _canonical(evidence)
        output.append({
            **evidence,
            "evidence_json": raw,
            "evidence_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        })
    return output


def _rehash(row):
    evidence = {key: row[key] for key in EVIDENCE_COLUMNS}
    row["evidence_json"] = _canonical(evidence)
    row["evidence_sha256"] = hashlib.sha256(
        row["evidence_json"].encode()
    ).hexdigest()


def test_v22_protocol_and_feature_identity_are_frozen_and_dormant():
    protocol = v22.load_protocol()
    evaluator = v22.load_evaluator_contract()
    status = v22.status()
    assert protocol["protocol_id"] == identity.PROTOCOL_ID
    assert len(features.BASE_FEATURE_NAMES) == identity.BASE_FEATURE_COUNT == 62
    assert len(features.REST_FEATURE_NAMES) == identity.ADDED_FEATURE_COUNT == 29
    assert len(features.FEATURE_NAMES) == identity.FEATURE_COUNT == 91
    assert features.FEATURE_NAMES_SHA256 == identity.FEATURE_NAMES_SHA256
    assert evaluator["contract_id"] == identity.EVALUATOR_CONTRACT_ID
    assert evaluator["partitions"] == {
        "exclusive_earliest_complete_windows": 180,
        "train": [0, 104],
        "probability_calibration": [105, 129],
        "execution_policy_selection": [130, 154],
        "untouched_test": [155, 179],
        "same_close_all_asset_cluster_may_not_cross_partitions": True,
        "probability_calibration_and_policy_selection_are_disjoint": True,
        "untouched_test_is_only_independent_final_historical_confirmation": True,
    }
    assert evaluator["base_feature_ablation"]["feature_count"] == 62
    assert evaluator["untouched_test"]["one_shot_only"] is True
    assert not any(
        "timestamp" in name or "latency" in name or "provider" in name
        for name in features.REST_FEATURE_NAMES
    )
    assert status["outcome_labels_read"] is False
    assert status["model_fit_performed"] is False
    assert status["notification_eligible"] is False
    assert status["real_trading_allowed"] is False


def test_v22_builds_exact_105_features_from_four_hash_bound_stages():
    parent, intermediate, delayed = _triplet()
    result = features.build_features(
        parent, intermediate, delayed, list(reversed(_rest_rows(parent))),
    )
    assert result["feature_count"] == 91
    assert len(result["features"]) == 91
    assert result["feature_names"] == list(features.FEATURE_NAMES)
    assert set(result["rest_evidence_sha256_by_stage"]) == set(features.STAGES)
    assert len(result["feature_evidence_sha256"]) == 64
    assert result["outcome_labels_read"] is False
    assert result["probability_scoring_performed"] is False
    assert result["real_trading_allowed"] is False


def test_v22_excludes_every_coinbase_spot_derived_v21_feature():
    assert len(features.EXCLUDED_SPOT_DERIVED_FEATURE_NAMES) == 14
    assert not set(features.BASE_FEATURE_NAMES) & set(
        features.EXCLUDED_SPOT_DERIVED_FEATURE_NAMES
    )
    assert set(features.BASE_FEATURE_NAMES) | set(
        features.EXCLUDED_SPOT_DERIVED_FEATURE_NAMES
    ) == set(features.v21_features.FEATURE_NAMES)


def test_v22_replaces_unusable_spot_source_without_changing_frozen_v21():
    parent, intermediate, delayed = _triplet()
    for row in (intermediate, delayed):
        row["threshold_json"]["spot_depth_status"] = "unavailable"
    frozen = v21_source.evaluate_triplet(parent, intermediate, delayed)
    assert frozen["eligible_for_v21_feature_credit"] is False
    result = features.build_features(
        parent, intermediate, delayed, _rest_rows(parent),
    )
    assert result["feature_count"] == identity.FEATURE_COUNT
    assert "SPOT_DEPTH_SOURCE_UNUSABLE" in result[
        "replaced_spot_source_failures"
    ]


def test_v22_excluded_spot_values_cannot_change_features_or_evidence_hash():
    parent, intermediate, delayed = _triplet()
    control = features.build_features(
        parent, intermediate, delayed, _rest_rows(parent),
    )
    for row in (intermediate, delayed):
        for index, key in enumerate(features.NEUTRAL_SPOT_KEYS):
            row["threshold_json"][key] = 999_999.0 + index
    challenger = features.build_features(
        parent, intermediate, delayed, _rest_rows(parent),
    )
    assert challenger["features"] == control["features"]
    assert challenger["feature_evidence_sha256"] == control[
        "feature_evidence_sha256"
    ]


def test_v22_rejects_any_outcome_or_label_input_even_if_features_are_valid():
    parent, intermediate, delayed = _triplet()
    delayed["threshold_json"]["label_survives"] = 1
    with pytest.raises(ValueError, match="outcome_or_label_input_forbidden"):
        features.build_features(
            parent, intermediate, delayed, _rest_rows(parent),
        )


def test_v22_fails_closed_on_missing_duplicate_or_misaligned_stage():
    parent, intermediate, delayed = _triplet()
    rows = _rest_rows()
    with pytest.raises(ValueError, match="stage_geometry"):
        features.build_features(parent, intermediate, delayed, rows[:-1])
    with pytest.raises(ValueError, match="source_identity"):
        features.build_features(
            parent, intermediate, delayed, [*rows[:-1], deepcopy(rows[0])],
        )
    bad = deepcopy(rows)
    bad[0]["ticker"] = "WRONG"
    _rehash(bad[0])
    with pytest.raises(ValueError, match="source_identity"):
        features.build_features(parent, intermediate, delayed, bad)


def test_v22_fails_closed_on_timing_evidence_or_book_tampering():
    parent, intermediate, delayed = _triplet()
    timing = _rest_rows()
    timing[0]["request_started_at"] += 3.0
    timing[0]["request_start_offset_seconds"] += 3.0
    timing[0]["response_latency_seconds"] -= 3.0
    _rehash(timing[0])
    with pytest.raises(ValueError, match="timestamp_alignment"):
        features.build_features(parent, intermediate, delayed, timing)

    row_evidence = _rest_rows()
    row_evidence[0]["submitted_at"] += 0.01
    with pytest.raises(ValueError, match="source_identity"):
        features.build_features(parent, intermediate, delayed, row_evidence)

    book = _rest_rows()
    book[0]["top_imbalance"] = 0.99
    _rehash(book[0])
    with pytest.raises(ValueError, match="book_geometry"):
        features.build_features(parent, intermediate, delayed, book)


def test_v22_protocol_tampering_fails_even_when_json_remains_valid(tmp_path):
    path = tmp_path / "protocol.json"
    protocol = v22.load_protocol()
    protocol["safety"]["real_trading_allowed_now"] = True
    path.write_text(json.dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256_mismatch"):
        v22.load_protocol(path)


def test_v22_evaluator_tampering_fails_before_any_label_access(tmp_path):
    path = tmp_path / "evaluator.json"
    contract = v22.load_evaluator_contract()
    contract["execution_policy_selection"]["edge_margin_grid"].append(0.08)
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="sha256_mismatch"):
        v22.load_evaluator_contract(path)
