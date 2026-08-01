from decimal import Decimal

from tools.q15_rti_improvement_audit import (
    HISTORICAL_FREEZE_CLOSE_TIME,
    _candidate_functions,
    _net_pnl_per_contract,
    audit,
)


def _example(*, row_id: int, close_time: float, asset: str, correct: bool) -> dict:
    return {
        "id": row_id,
        "asset": asset,
        "ticker": f"{asset}-{int(close_time)}",
        "close_time": close_time,
        "decision_time": close_time - 780.0,
        "side": "YES",
        "official_result": "YES" if correct else "NO",
        "correct": int(correct),
        "ask": 60.0,
        "spread": 1.0,
        "control": True,
        "strong_wide": True,
        "value_wide": True,
        "distance_into_side_bps": 2.0,
        "side_move_bps": 1.0,
        "persistence": 0.98,
        "path_crossings": 0,
        "seconds_since_cross": 60.0,
        "path_range_bps": 2.0,
        "realized_volatility_bps": 1.0,
        "momentum_acceleration_bps": 0.2,
        "volatility_normalized_margin": 0.3,
        "spot_snapshot_age_seconds": 1.0,
        "spot_book_age_seconds": 0.5,
        "spot_depth_imbalance": 0.2,
        "spot_trade_net_notional_15s": 10.0,
        "spot_fresh": True,
        "coinbase_snapshot_age_seconds": None,
        "coinbase_depth_imbalance": None,
    }


def test_official_fee_and_two_cent_slippage_are_applied_to_ten_lot_basis():
    # 60c YES winner: 40c gross - 1.7c official 10-lot fee allocation - 2c slip.
    assert _net_pnl_per_contract(60.0, True) == Decimal("36.32")
    assert _net_pnl_per_contract(60.0, False) == Decimal("-63.68")


def test_spot_candidate_fails_closed_on_direction_or_freshness():
    predicate = _candidate_functions()["spot_book_confirm_v1"]
    row = _example(row_id=1, close_time=1_000.0, asset="BTC", correct=True)
    assert predicate(row)

    row["spot_depth_imbalance"] = 0.0
    assert not predicate(row)
    row["spot_depth_imbalance"] = -0.1
    assert not predicate(row)
    row["spot_depth_imbalance"] = 0.1
    row["spot_fresh"] = False
    assert not predicate(row)


def test_chronological_folds_keep_assets_from_one_close_together():
    examples = []
    row_id = 0
    for window in range(30):
        close_time = 10_000.0 + 900.0 * window
        for asset in ("BTC", "ETH"):
            row_id += 1
            examples.append(
                _example(
                    row_id=row_id,
                    close_time=close_time,
                    asset=asset,
                    correct=(row_id % 2 == 0),
                )
            )

    examples.append(
        _example(
            row_id=10_000,
            close_time=HISTORICAL_FREEZE_CLOSE_TIME + 900.0,
            asset="BTC",
            correct=True,
        )
    )

    report = audit(examples, {"delayed_kalshi_quote_within_10s": {"30": 0, "60": 0}})
    assert report["split"] == {
        "method": "chronological_60_20_20_grouped_by_close_time",
        "train_windows": 18,
        "calibration_windows": 6,
        "test_windows": 6,
        "test_first_close": 31_600.0,
        "test_last_close": 36_100.0,
    }
    folds = report["candidates"]["spot_book_confirm_v1"]["folds"]
    assert folds["train"]["n"] == 36
    assert folds["calibration"]["n"] == 12
    assert folds["test"]["n"] == 12
    assert report["selection_integrity"]["historical_final_fold_is_truly_untouched"] is False
    assert report["coverage"]["post_freeze_reconstructed_windows"] == 1
    assert report["post_freeze_reconstruction_not_promotion_evidence"][
        "spot_book_confirm_v1"
    ]["n"] == 1
    assert report["selected_shadow"]["promotion_eligible"] is False
    assert set(report["selected_breakdowns"]["by_transfer_cohort"]) == {
        "BTC",
        "NON_BTC_TRANSFER",
    }
