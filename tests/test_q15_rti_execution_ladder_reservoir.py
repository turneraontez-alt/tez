from __future__ import annotations

from copy import deepcopy
import json

from q15_upgrade.strategy_bots import rti_execution_ladder_reservoir_identity as identity
from q15_upgrade.strategy_bots.rules import (
    RTI_EXECUTION_LADDER_RESERVOIR_KEYS,
)
from tools import q15_rti_execution_ladder_reservoir_readiness as readiness
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _rows(*, full: bool = True) -> list[dict]:
    rows = []
    for index, asset in enumerate(ASSETS, start=1):
        profile = {
            "rti_execution_ladder_schema_version": identity.SCHEMA_VERSION,
            "rti_ladder_depth_within_2c_contracts": 12.0 if full else 6.0,
            "rti_ladder_10_contract_filled_contracts": 10.0 if full else 6.0,
            "rti_ladder_10_contract_full_fill_supported": full,
            "rti_ladder_10_contract_vwap_cents": 51.0 if full else None,
            "rti_ladder_10_contract_worst_price_cents": 52.0 if full else None,
            "rti_ladder_10_contract_slippage_cents": 1.0 if full else None,
            "quote_age_seconds": 0.2,
            "quote_evidence_source": "kalshi_official_websocket_book",
        }
        rows.append({
            "id": index,
            "asset": asset,
            "close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
            "interval": "12M",
            "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
            "entry_ask_cents": 50.0,
            "depth_contracts": 12.0 if full else 6.0,
            "threshold_json": profile,
        })
    return rows


def test_protocol_is_frozen_record_only_and_hash_bound():
    protocol = readiness.load_protocol()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert protocol["usage"]["used_by_v21"] is False
    assert protocol["usage"]["outcome_access_allowed"] is False
    assert protocol["usage"]["real_trading_allowed"] is False


def test_runtime_serializer_and_readiness_require_the_same_ladder_schema():
    assert RTI_EXECUTION_LADDER_RESERVOIR_KEYS == readiness.FIELDS


def test_complete_full_and_partial_ladders_receive_honest_credit():
    rows = _rows()
    rows[-1] = _rows(full=False)[-1]
    report = readiness.build_readiness(rows)
    assert report["usable_ladder_complete_close_windows"] == 1
    assert report["full_fill_supported_rows"] == 6
    assert report["top_of_book_full_fill_supported_rows"] == 6
    assert report["ladder_recovered_full_fill_rows"] == 0
    assert report["quality_failure_counts"] == {}
    assert report["outcome_labels_read"] is False


def test_partial_fill_with_fake_vwap_fails_entire_window():
    rows = _rows(full=False)
    profile = dict(rows[0]["threshold_json"])
    profile["rti_ladder_10_contract_vwap_cents"] = 50.0
    rows[0]["threshold_json"] = json.dumps(profile)
    report = readiness.build_readiness(rows)
    assert report["usable_ladder_complete_close_windows"] == 0
    assert report["quality_failure_counts"][
        "LADDER_PARTIAL_FILL_HAS_FAKE_PRICE"
    ] == 1


def test_stale_or_tampered_full_fill_fails_closed():
    rows = deepcopy(_rows())
    profile = dict(rows[0]["threshold_json"])
    profile["quote_age_seconds"] = 4.0
    profile["rti_ladder_10_contract_worst_price_cents"] = 53.0
    rows[0]["threshold_json"] = profile
    report = readiness.build_readiness(rows)
    assert report["usable_ladder_complete_close_windows"] == 0
    assert report["quality_failure_counts"]["LADDER_OFFICIAL_QUOTE_NOT_FRESH"] == 1
    assert report["quality_failure_counts"]["LADDER_FULL_FILL_CONTRADICTION"] == 1


def test_ladder_recovery_is_reported_without_changing_top_book_control():
    rows = _rows()
    rows[0]["depth_contracts"] = 6.0
    report = readiness.build_readiness(rows)
    assert report["top_of_book_full_fill_supported_rows"] == 6
    assert report["full_fill_supported_rows"] == 7
    assert report["ladder_recovered_full_fill_rows"] == 1
    assert report["ladder_recovered_full_fill_rows_by_asset"] == {"BNB": 1}
