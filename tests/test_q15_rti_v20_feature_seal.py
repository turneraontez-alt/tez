from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v20_features as features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from tools import q15_rti_v20_feature_seal as seal


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EASTERN = ZoneInfo("America/New_York")


def _ticker(asset: str, close_time: float) -> str:
    close = datetime.fromtimestamp(close_time, tz=EASTERN)
    month = close.strftime("%b").upper()
    return (
        f"KX{asset}15M-{close:%y}{month}{close:%d%H%M}-{close:%M}"
    )


def _row(window_index: int, asset_index: int, close_time: float) -> dict:
    asset = ASSETS[asset_index]
    parent_id = window_index * 100 + asset_index + 1
    delayed_id = 1_000_000 + parent_id
    values = [
        float(window_index) + asset_index / 10.0 + index / 1000.0
        for index in range(identity.FEATURE_COUNT)
    ]
    delayed_source_hash = seal._canonical_sha256({
        "window": window_index,
        "asset": asset,
        "source": "official-point-in-time",
    })
    ticker = _ticker(asset, close_time)
    side = "YES" if (window_index + asset_index) % 2 == 0 else "NO"
    feature_hash = seal._canonical_sha256({
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": parent_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "source_feature_evidence_sha256": delayed_source_hash,
        "feature_names": list(features.FEATURE_NAMES),
        "features": values,
    })
    source_hash = seal._canonical_sha256({
        "parent_id": parent_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_evidence_sha256": feature_hash,
        "feature_count": identity.FEATURE_COUNT,
    })
    v18_hash = seal._canonical_sha256({"parent_id": parent_id, "benchmark": "V18"})
    v19_hash = seal._canonical_sha256({"parent_id": parent_id, "benchmark": "V19"})
    benchmark = {
        "matched_v18_eligible": asset != "BTC" and window_index % 5 == 0,
        "matched_v18_feature_evidence_sha256": v18_hash,
        "matched_v19_eligible": asset != "BTC" and window_index % 11 == 0,
        "matched_v19_feature_evidence_sha256": v19_hash,
    }
    return {
        "parent_id": parent_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "entry_ask_cents": 50.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "sim_contracts": 10.0,
        "sim_full_fill_supported": True,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "delayed_source_evidence_sha256": delayed_source_hash,
        "feature_evidence_sha256": feature_hash,
        "source_feature_evidence_sha256": source_hash,
        **benchmark,
        "matched_benchmark_evidence_sha256": seal._canonical_sha256(benchmark),
        "features": values,
    }


def _windows(count: int = 150, *, start_offset: int = 0) -> list[dict]:
    output = []
    for offset in range(count):
        index = start_offset + offset
        close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME + index * 900.0
        output.append({
            "close_time": close_time,
            "rows": [
                _row(index, asset_index, close_time)
                for asset_index in range(len(ASSETS))
            ],
        })
    return output


def test_feature_seal_freezes_earliest_disjoint_90_30_30_without_labels():
    payload = seal.build_seal(_windows())
    result = seal.validate_seal(payload)
    assert result["valid"] is True
    assert payload["selected_complete_close_windows"] == 150
    assert payload["selected_rows"] == 1050
    assert len(payload["partition_windows"]["TRAIN"]) == 90
    assert len(payload["partition_windows"]["CALIBRATION"]) == 30
    assert len(payload["partition_windows"]["UNTOUCHED_TEST"]) == 30
    assert set(payload["partition_windows"]["TRAIN"]).isdisjoint(
        payload["partition_windows"]["CALIBRATION"]
    )
    assert set(payload["partition_windows"]["CALIBRATION"]).isdisjoint(
        payload["partition_windows"]["UNTOUCHED_TEST"]
    )
    assert payload["outcome_columns_selected"] is False
    assert payload["outcome_labels_read"] is False
    assert payload["model_fit_performed"] is False


def test_feature_seal_is_stable_when_later_windows_arrive():
    first = seal.build_seal(_windows(150))
    later = seal.build_seal(_windows(151))
    assert first == later


def test_feature_seal_fails_closed_on_tamper_partition_and_outcome_field():
    payload = seal.build_seal(_windows())
    tampered = deepcopy(payload)
    tampered["rows"][0]["features"][0] += 1.0
    with pytest.raises(ValueError, match="row_hash_mismatch"):
        seal.validate_seal(tampered)

    tampered = deepcopy(payload)
    tampered["rows"][0]["partition"] = "UNTOUCHED_TEST"
    tampered["seal_sha256"] = seal._canonical_sha256(
        seal._seal_core(tampered)
    )
    with pytest.raises(ValueError, match="cross_partition"):
        seal.validate_seal(tampered)

    tampered = deepcopy(payload)
    tampered["rows"][0]["correct"] = True
    tampered["seal_sha256"] = seal._canonical_sha256(
        seal._seal_core(tampered)
    )
    with pytest.raises(ValueError, match="outcome_field"):
        seal.validate_seal(tampered)


def test_feature_seal_create_once_is_idempotent_and_conflict_exclusive(tmp_path):
    output = tmp_path / "seal.json"
    candidate = seal.build_seal(_windows())
    first = seal.create_or_validate_seal(candidate, output)
    second = seal.create_or_validate_seal(candidate, output)
    assert first["created"] is True
    assert second["created"] is False

    shifted = seal.build_seal(_windows(start_offset=1))
    with pytest.raises(ValueError, match="exclusive_feature_seal_conflict"):
        seal.create_or_validate_seal(shifted, output)


def test_feature_seal_cannot_be_built_early():
    with pytest.raises(ValueError, match="not_ready"):
        seal.build_seal(_windows(149))
