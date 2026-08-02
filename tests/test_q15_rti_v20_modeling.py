from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v20_features as features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from tools import q15_rti_v20_feature_seal as seal
from tools import q15_rti_v20_modeling as modeling


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EASTERN = ZoneInfo("America/New_York")


def _ticker(asset: str, close_time: float) -> str:
    close = datetime.fromtimestamp(close_time, tz=EASTERN)
    return (
        f"KX{asset}15M-{close:%y}{close:%b}".upper()
        + f"{close:%d%H%M}-{close:%M}"
    )


def _row(window_index: int, asset_index: int, close_time: float) -> tuple[dict, int]:
    asset = ASSETS[asset_index]
    parent_id = window_index * 100 + asset_index + 1
    delayed_id = 1_000_000 + parent_id
    side = "YES" if (window_index + asset_index) % 2 == 0 else "NO"
    label = 1 if window_index % 4 in {0, 1} else 0
    values = [0.0] * identity.FEATURE_COUNT
    values[0] = 1.0 if side == "YES" else 0.0
    values[1] = (3.0 if label else -3.0) + asset_index / 100.0
    values[2] = window_index / 150.0
    values[45] = 0.51
    asset_feature = {
        "BNB": 46, "DOGE": 47, "ETH": 48,
        "HYPE": 49, "SOL": 50, "XRP": 51,
    }.get(asset)
    if asset_feature is not None:
        values[asset_feature] = 1.0
    delayed_source_hash = seal._canonical_sha256({
        "window": window_index,
        "asset": asset,
        "source": "official-point-in-time",
    })
    ticker = _ticker(asset, close_time)
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
    cohort = "BTC" if asset == "BTC" else "NON_BTC_TRANSFER"
    source_hash = seal._canonical_sha256({
        "parent_id": parent_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
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
    return ({
        "parent_id": parent_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "entry_ask_cents": 40.0,
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
    }, label)


def _sealed_population() -> tuple[dict, dict[int, int]]:
    windows = []
    labels = {}
    for window_index in range(150):
        close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME + window_index * 900.0
        rows = []
        for asset_index in range(7):
            row, label = _row(window_index, asset_index, close_time)
            rows.append(row)
            labels[row["parent_id"]] = label
        windows.append({"close_time": close_time, "rows": rows})
    return seal.build_seal(windows), labels


@pytest.fixture(scope="module")
def population():
    return _sealed_population()


def _pretest_labels(payload: dict, labels: dict[int, int]) -> dict[int, int]:
    return {
        int(row["parent_id"]): labels[int(row["parent_id"])]
        for row in payload["rows"]
        if row["partition"] in {"TRAIN", "CALIBRATION"}
    }


def _small_contract() -> dict:
    contract = deepcopy(modeling.load_contract())
    contract["candidates"]["NON_BTC_TRANSFER"] = [
        contract["candidates"]["NON_BTC_TRANSFER"][0]
    ]
    contract["candidates"]["BTC"] = [
        contract["candidates"]["BTC"][0]
    ]
    execution = contract["selective_execution"]
    execution["bootstrap_resamples"] = 200
    execution["non_btc_minimum_picks"] = 5
    execution["btc_minimum_picks"] = 2
    execution["minimum_yes_picks"] = 1
    execution["minimum_no_picks"] = 1
    return contract


def test_contract_is_hash_bound_and_contains_every_frozen_candidate():
    contract = modeling.load_contract()
    assert len(contract["candidates"]["NON_BTC_TRANSFER"]) == 28
    assert len(contract["candidates"]["BTC"]) == 4
    assert contract["safety"]["automatic_promotion"] is False
    assert contract["safety"]["real_trading_allowed"] is False


def test_pretest_rejects_any_untouched_test_label_before_model_fit(
    population, monkeypatch,
):
    payload, all_labels = population
    labels = _pretest_labels(payload, all_labels)
    test_row = next(
        row for row in payload["rows"]
        if row["partition"] == "UNTOUCHED_TEST"
    )
    labels[int(test_row["parent_id"])] = all_labels[int(test_row["parent_id"])]
    monkeypatch.setattr(
        modeling,
        "evaluate_cohort",
        lambda *_args, **_kwargs: pytest.fail("model fit occurred before rejection"),
    )
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling.evaluate_pretest(payload, labels)


def test_frozen_modeling_runs_cohorts_separately_and_selects_calibration_margin(
    population,
):
    payload, all_labels = population
    rows = modeling._labeled_pretest_rows(
        payload, _pretest_labels(payload, all_labels)
    )
    contract = _small_contract()
    non_btc = modeling.evaluate_cohort(rows, "NON_BTC_TRANSFER", contract)
    btc = modeling.evaluate_cohort(rows, "BTC", contract)
    for result, expected_train, expected_calibration in (
        (non_btc, 540, 180),
        (btc, 90, 30),
    ):
        report = result["report"]
        assert report["internal_walk_forward_gate_met"] is True
        assert report["calibration_gate_met"] is True
        assert report["pretest_gate_met"] is True
        assert report["train_rows"] == expected_train
        assert report["calibration_rows"] == expected_calibration
        assert report["calibration_in_sample_not_independent_confirmation"] is True
        assert result["artifact"] is not None
        assert report["selected_margin"]["gate_met"] is True


def test_internal_walk_forward_cannot_see_calibration_features(population):
    payload, all_labels = population
    rows = modeling._labeled_pretest_rows(
        payload, _pretest_labels(payload, all_labels)
    )
    cohort_rows = [
        row for row in rows if row["cohort"] == "NON_BTC_TRANSFER"
    ]
    mutated = deepcopy(cohort_rows)
    for row in mutated:
        if row["partition"] == "CALIBRATION":
            row["features"] = [value + 1_000_000.0 for value in row["features"]]
    contract = _small_contract()
    spec = contract["candidates"]["NON_BTC_TRANSFER"][0]
    first = modeling._candidate_walk_forward(cohort_rows, spec, contract)
    second = modeling._candidate_walk_forward(mutated, spec, contract)
    assert first == second


def test_pretest_label_set_is_exactly_840_train_calibration_rows(population):
    payload, all_labels = population
    labels = _pretest_labels(payload, all_labels)
    rows = modeling._labeled_pretest_rows(payload, labels)
    assert len(labels) == 840
    assert len(rows) == 840
    assert all(row["partition"] != "UNTOUCHED_TEST" for row in rows)
