from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v21_features as features
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools import q15_rti_v21_feature_seal as seal


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EASTERN = ZoneInfo("America/New_York")


def _ticker(asset: str, close_time: float) -> str:
    close = datetime.fromtimestamp(close_time, tz=EASTERN)
    return (
        f"KX{asset}15M-{close:%y}{close:%b}".upper()
        + f"{close:%d%H%M}-{close:%M}"
    )


def _row(window_index: int, asset_index: int, close_time: float) -> dict:
    asset = ASSETS[asset_index]
    parent_id = window_index * 100 + asset_index + 1
    intermediate_id = 1_000_000 + parent_id
    delayed_id = 2_000_000 + parent_id
    side = "YES" if (window_index + asset_index) % 2 == 0 else "NO"
    values = [
        float(window_index) + asset_index / 10.0 + index / 1000.0
        for index in range(identity.FEATURE_COUNT)
    ]
    ticker = _ticker(asset, close_time)
    base_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "source": "v20-base",
    })
    intermediate_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "stage": 30,
    })
    delayed_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "stage": 60,
    })
    feature_hash = seal._canonical_sha256({
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "base_feature_evidence_sha256": base_hash,
        "feature_names": list(features.FEATURE_NAMES),
        "features": values,
    })
    cohort = "BTC" if asset == "BTC" else "NON_BTC_TRANSFER"
    execution_supported = (window_index + asset_index) % 4 != 0
    source_hash = seal._canonical_sha256({
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_evidence_sha256": feature_hash,
        "base_feature_evidence_sha256": base_hash,
        "intermediate_source_evidence_sha256": intermediate_hash,
        "delayed_source_evidence_sha256": delayed_hash,
        "feature_count": identity.FEATURE_COUNT,
        "execution_supported": execution_supported,
        "entry_ask_cents": 50.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0 if execution_supported else 4.0,
        "sim_contracts": 10.0,
    })
    v18_hash = seal._canonical_sha256({"parent": parent_id, "benchmark": "v18"})
    v19_hash = seal._canonical_sha256({"parent": parent_id, "benchmark": "v19"})
    benchmarks = {
        "matched_v18_eligible": asset != "BTC" and window_index % 5 == 0,
        "matched_v18_feature_evidence_sha256": v18_hash,
        "matched_v19_eligible": asset != "BTC" and window_index % 11 == 0,
        "matched_v19_feature_evidence_sha256": v19_hash,
        "v20_base_feature_evidence_sha256": base_hash,
    }
    return {
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "execution_supported": execution_supported,
        "entry_ask_cents": 50.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0 if execution_supported else 4.0,
        "sim_contracts": 10.0,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "intermediate_source_evidence_sha256": intermediate_hash,
        "delayed_source_evidence_sha256": delayed_hash,
        "feature_evidence_sha256": feature_hash,
        "source_feature_evidence_sha256": source_hash,
        **benchmarks,
        "matched_benchmark_evidence_sha256": seal._canonical_sha256(benchmarks),
        "features": values,
    }


def _windows(count: int = 180, *, start_offset: int = 0) -> list[dict]:
    output = []
    for offset in range(count):
        index = start_offset + offset
        close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME + index * 900.0
        output.append({
            "close_time": close_time,
            "rows": [_row(index, asset_index, close_time) for asset_index in range(7)],
        })
    return output


def test_v21_seal_freezes_disjoint_105_25_25_25_and_preserves_nonfills():
    payload = seal.build_seal(_windows())
    result = seal.validate_seal(payload)
    assert result["valid"] is True
    assert payload["selected_complete_close_windows"] == 180
    assert payload["selected_rows"] == 1260
    assert len(payload["partition_windows"]["TRAIN"]) == 105
    assert len(payload["partition_windows"]["PROBABILITY_CALIBRATION"]) == 25
    assert len(payload["partition_windows"]["EXECUTION_POLICY_SELECTION"]) == 25
    assert len(payload["partition_windows"]["UNTOUCHED_TEST"]) == 25
    assert result["executable_rows"] < result["selected_rows"]
    assert payload["feature_credit_requires_all_rows_executable"] is False
    assert payload["pnl_credit_requires_row_level_execution_supported"] is True
    assert payload["outcome_labels_read"] is False


def test_v21_seal_is_stable_when_later_windows_arrive_and_fails_early():
    first = seal.build_seal(_windows(180))
    later = seal.build_seal(_windows(181))
    assert first == later
    with pytest.raises(ValueError, match="not_ready"):
        seal.build_seal(_windows(179))


def test_v21_seal_rejects_feature_execution_partition_and_outcome_tamper():
    payload = seal.build_seal(_windows())
    tampered = deepcopy(payload)
    tampered["rows"][0]["features"][0] += 1.0
    with pytest.raises(ValueError, match="row_hash_mismatch"):
        seal.validate_seal(tampered)

    tampered = deepcopy(payload)
    tampered["rows"][0]["execution_supported"] = not tampered["rows"][0][
        "execution_supported"
    ]
    with pytest.raises(ValueError, match="row_hash_mismatch"):
        seal.validate_seal(tampered)

    tampered = deepcopy(payload)
    tampered["rows"][0]["partition"] = "UNTOUCHED_TEST"
    with pytest.raises(ValueError, match="row_identity"):
        seal.validate_seal(tampered)

    tampered = deepcopy(payload)
    tampered["rows"][0]["correct"] = True
    with pytest.raises(ValueError, match="outcome_field"):
        seal.validate_seal(tampered)


def test_v21_seal_create_once_is_idempotent_and_exclusive(tmp_path):
    output = tmp_path / "feature_seal.json"
    candidate = seal.build_seal(_windows())
    assert seal.create_or_validate_seal(candidate, output)["created"] is True
    assert seal.create_or_validate_seal(candidate, output)["created"] is False
    shifted = seal.build_seal(_windows(start_offset=1))
    with pytest.raises(ValueError, match="exclusive_feature_seal_conflict"):
        seal.create_or_validate_seal(shifted, output)
