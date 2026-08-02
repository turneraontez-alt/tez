from __future__ import annotations

import copy
import hashlib
import json

from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity
from q15_upgrade.strategy_bots import (
    rti_microstructure_v22_top_book_features as features,
)
from tools import q15_rti_v22_feature_quality as quality


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _sha(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _row(asset: str, close: float, offset: int) -> dict:
    values = [float((index + 1) * (offset + 1)) for index in range(91)]
    row = {
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": 1000 + offset,
        "asset": asset,
        "ticker": f"KX{asset}-{int(close)}",
        "close_time": close,
        "side": "YES" if offset % 2 == 0 else "NO",
        "parent_source_evidence_sha256": "a" * 64,
        "intermediate_source_evidence_sha256": "b" * 64,
        "delayed_source_evidence_sha256": "c" * 64,
        "rest_evidence_sha256_by_stage": {
            "13M": "d" * 64,
            "12M30S": "e" * 64,
            "12M": "f" * 64,
            "11M30S": "0" * 64,
        },
        "feature_names": list(features.FEATURE_NAMES),
        "features": values,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
    }
    row["feature_evidence_sha256"] = _sha(quality._row_evidence_core(row))
    return row


def _windows(count: int = 2):
    output = []
    offset = 0
    for window_index in range(count):
        close = 1800.0 + 900.0 * window_index
        rows = []
        for asset in ASSETS:
            rows.append(_row(asset, close, offset))
            offset += 1
        output.append({"close_time": close, "rows": rows})
    return output


def test_quality_report_accepts_complete_outcome_blind_structure():
    report = quality.build_quality_report(_windows())
    assert report["status"] == "PASS_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
    assert report["complete_close_windows"] == 2
    assert report["feature_rows"] == 14
    assert report["feature_count"] == 91
    assert report["structural_failure_counts"] == {}
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False


def test_quality_report_rehashes_every_feature_row():
    windows = _windows()
    windows[0]["rows"][0]["features"][5] += 0.25
    report = quality.build_quality_report(windows)
    assert report["status"] == "FAIL_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
    assert report["structural_failure_counts"] == {
        "ROW_FEATURE_EVIDENCE_HASH_INVALID": 1,
    }


def test_quality_report_detects_identity_and_window_tampering():
    windows = _windows()
    bad = copy.deepcopy(windows[0]["rows"][0])
    bad["feature_names_sha256"] = "1" * 64
    windows[0]["rows"][0] = bad
    report = quality.build_quality_report(windows)
    assert report["status"] == "FAIL_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
    assert report["structural_failure_counts"] == {
        "ROW_FEATURE_IDENTITY_INVALID": 1,
    }


def test_quality_report_surfaces_source_failures_without_labels():
    report = quality.build_quality_report(
        _windows(),
        excluded_windows=[{"failure_counts": {"BROKEN_WINDOW": 1}}],
        rest_failure_counts={"REST_STALE": 2},
    )
    assert report["status"] == "FAIL_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
    assert report["excluded_window_failure_counts"] == {"BROKEN_WINDOW": 1}
    assert report["rest_quality_failure_counts"] == {"REST_STALE": 2}
    assert report["probability_scoring_performed"] is False
