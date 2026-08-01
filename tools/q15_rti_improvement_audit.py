"""Leakage-safe audit for the exact RTI Path 13M control and challengers."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_CEILING
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence


KNOWN_LOSSES = {
    ("BTC", 1784421000.0),
    ("BTC", 1784424600.0),
    ("HYPE", 1784426400.0),
}
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
PRODUCTS = {asset: f"{asset}-USD" for asset in ASSETS}
HISTORICAL_FREEZE_CLOSE_TIME = 1784432700.0


def _json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _net_pnl_per_contract(ask_cents: float, correct: bool) -> Decimal:
    ask = Decimal(str(ask_cents))
    probability = ask / Decimal("100")
    position_cost = Decimal("10") * probability
    raw_fee = Decimal("0.07") * Decimal("10") * probability * (
        Decimal("1") - probability
    )
    rounded_total = (position_cost + raw_fee).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING
    )
    fee_order = (rounded_total - position_cost) * Decimal("100")
    gross = Decimal("100") - ask if correct else -ask
    return gross - fee_order / Decimal("10") - Decimal("2")


def _wilson(correct: int, n: int) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    z = 1.959963984540054
    p = correct / n
    denominator = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denominator
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _metrics(rows: Sequence[Mapping[str, Any]], *, total_windows: int) -> dict[str, Any]:
    materialized = list(rows)
    correct = sum(int(row["correct"]) for row in materialized)
    pnls = [
        _net_pnl_per_contract(float(row["ask"]), bool(row["correct"]))
        for row in materialized
    ]
    equity = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    low, high = _wilson(correct, len(materialized))
    total = sum(pnls, Decimal("0"))
    fee_break_evens = []
    fee_slippage_break_evens = []
    for row in materialized:
        ask = Decimal(str(row["ask"]))
        probability = ask / Decimal("100")
        position_cost = Decimal("10") * probability
        raw_fee = Decimal("0.07") * Decimal("10") * probability * (
            Decimal("1") - probability
        )
        rounded_total = (position_cost + raw_fee).quantize(
            Decimal("0.0001"), rounding=ROUND_CEILING
        )
        fee_per_contract_cents = (
            (rounded_total - position_cost) * Decimal("100") / Decimal("10")
        )
        fee_break_evens.append(
            float((ask + fee_per_contract_cents) / Decimal("100"))
        )
        fee_slippage_break_evens.append(
            float(
                (ask + fee_per_contract_cents + Decimal("2")) / Decimal("100")
            )
        )
    return {
        "n": len(materialized),
        "correct": correct,
        "accuracy": None if not materialized else correct / len(materialized),
        "wilson_95_low": low,
        "wilson_95_high": high,
        "avg_fee_adjusted_break_even_rate": (
            None
            if not fee_break_evens
            else sum(fee_break_evens) / len(fee_break_evens)
        ),
        "avg_fee_slippage_adjusted_break_even_rate": (
            None
            if not fee_slippage_break_evens
            else sum(fee_slippage_break_evens) / len(fee_slippage_break_evens)
        ),
        "fee_slippage_adjusted_pnl_cents_per_contract": float(total),
        "ten_contract_net_pnl_dollars": float(total * Decimal("10") / Decimal("100")),
        "ev_cents_per_contract": None if not pnls else float(total / len(pnls)),
        "max_drawdown_cents_per_contract": None if not pnls else float(drawdown),
        "trade_frequency_per_window": (
            0.0 if total_windows <= 0 else len(materialized) / total_windows
        ),
        "assets": dict(sorted(Counter(str(row["asset"]) for row in materialized).items())),
        "provisional": len(materialized) < 30,
    }


def _nearest_before(
    conn: sqlite3.Connection,
    table: str,
    where_column: str,
    where_value: str,
    time_column: str,
    decision_time: float,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {where_column}=? AND {time_column}<=? "
        f"ORDER BY {time_column} DESC LIMIT 1",
        (where_value, decision_time),
    ).fetchone()
    return None if row is None else dict(row)


def reconstruct(
    *,
    strategy_db: str,
    settlement_db: str,
    spot_db: str,
    coinbase_db: str,
    v95_db: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    strategy = sqlite3.connect(strategy_db, timeout=30.0)
    strategy.row_factory = sqlite3.Row
    settlement = sqlite3.connect(settlement_db, timeout=30.0)
    settlement.row_factory = sqlite3.Row
    spot = sqlite3.connect(spot_db, timeout=30.0)
    spot.row_factory = sqlite3.Row
    coinbase = sqlite3.connect(coinbase_db, timeout=30.0)
    coinbase.row_factory = sqlite3.Row
    rejected: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    try:
        source_rows = strategy.execute(
            "SELECT * FROM strategy_bot_decisions WHERE bot_name='rti_path_13m' "
            "AND official_result IN ('YES','NO') ORDER BY close_time,id"
        ).fetchall()
        for source in source_rows:
            profile = _json(source["threshold_json"])
            strike = _num(profile.get("rti_strike"))
            start_px = _num(profile.get("rti_path_start_px"))
            end_px = _num(profile.get("rti_path_end_px"))
            ask = _num(source["entry_ask_cents"])
            spread = _num(source["spread_cents"])
            side = str(source["side"] or "").upper()
            if (
                strike is None or strike <= 0.0 or start_px is None or end_px is None
                or ask is None or spread is None or side not in {"YES", "NO"}
            ):
                rejected["source_evidence_missing"] += 1
                continue
            close_time = float(source["close_time"])
            decision_time = close_time - 780.0
            ticks = settlement.execute(
                "SELECT ts,index_px FROM settlement_index_ticks WHERE asset=? "
                "AND ts BETWEEN ? AND ? ORDER BY ts",
                (source["asset"], decision_time - 60.0, decision_time),
            ).fetchall()
            if len(ticks) < 50:
                rejected["rti_tick_coverage"] += 1
                continue
            times = [float(row["ts"]) for row in ticks]
            prices = [float(row["index_px"]) for row in ticks]
            sign = 1.0 if side == "YES" else -1.0
            states = [price >= strike for price in prices]
            crossings = sum(left != right for left, right in zip(states, states[1:]))
            last_cross = next(
                (times[idx] for idx in range(len(states) - 1, 0, -1) if states[idx] != states[idx - 1]),
                None,
            )
            returns = [
                10_000.0 * math.log(right / left)
                for left, right in zip(prices, prices[1:])
                if left > 0.0 and right > 0.0
            ]
            realized_volatility = math.sqrt(sum(value * value for value in returns))
            path_range = (max(prices) - min(prices)) / prices[-1] * 10_000.0
            half = max(1, len(prices) // 2)
            first_half = sign * (prices[half - 1] / prices[0] - 1.0) * 10_000.0
            second_half = sign * (prices[-1] / prices[half - 1] - 1.0) * 10_000.0
            spot_row = _nearest_before(
                spot, "spot_depth_snapshots", "asset", str(source["asset"]),
                "created_at", decision_time,
            )
            coinbase_row = _nearest_before(
                coinbase, "coinbase_adv_l2_snapshots", "product_id",
                PRODUCTS[str(source["asset"])], "created_at", decision_time,
            )
            challengers = profile.get("challengers")
            challengers = challengers if isinstance(challengers, Mapping) else {}
            spot_snapshot_age = (
                None if spot_row is None else decision_time - float(spot_row["created_at"])
            )
            spot_book_age = None if spot_row is None else _num(spot_row.get("book_age_seconds"))
            spot_imbalance = None if spot_row is None else _num(spot_row.get("depth_imbalance"))
            spot_flow_15s = (
                None if spot_row is None
                else (_num(spot_row.get("trade_buy_notional_15s")) or 0.0)
                - (_num(spot_row.get("trade_sell_notional_15s")) or 0.0)
            )
            spot_fresh = bool(
                spot_snapshot_age is not None and 0.0 <= spot_snapshot_age <= 3.0
                and spot_book_age is not None and -3.0 <= spot_book_age <= 2.0
            )
            examples.append({
                "id": int(source["id"]),
                "asset": str(source["asset"]),
                "ticker": str(source["ticker"]),
                "close_time": close_time,
                "decision_time": decision_time,
                "side": side,
                "official_result": str(source["official_result"]),
                "correct": int(source["correct"] or 0),
                "ask": ask,
                "spread": spread,
                "kalshi_depth_contracts": _num(source["depth_contracts"]),
                "control": source["decision_status"] == "ACCEPTED",
                "strong_wide": bool((challengers.get("strong_path_wide_v1") or {}).get("accepted")),
                "value_wide": bool((challengers.get("value_price_wide_v1") or {}).get("accepted")),
                "distance_into_side_bps": sign * (end_px - strike) / strike * 10_000.0,
                "side_move_bps": _num(profile.get("rti_side_move_bps")) or 0.0,
                "persistence": _num(profile.get("rti_path_persistence")) or 0.0,
                "path_crossings": crossings,
                "seconds_since_cross": 60.0 if last_cross is None else times[-1] - last_cross,
                "path_range_bps": path_range,
                "realized_volatility_bps": realized_volatility,
                "momentum_acceleration_bps": second_half - first_half,
                "volatility_normalized_margin": (
                    sign * (end_px - strike) / strike * 10_000.0
                    / max(0.05, realized_volatility * math.sqrt(13.0))
                ),
                "spot_snapshot_age_seconds": spot_snapshot_age,
                "spot_book_age_seconds": spot_book_age,
                "spot_depth_imbalance": spot_imbalance,
                "spot_depth_total_notional": (
                    None
                    if spot_row is None
                    or _num(spot_row.get("bid_notional_levels")) is None
                    or _num(spot_row.get("ask_notional_levels")) is None
                    else float(spot_row["bid_notional_levels"])
                    + float(spot_row["ask_notional_levels"])
                ),
                "spot_trade_net_notional_15s": spot_flow_15s,
                "spot_fresh": spot_fresh,
                "coinbase_snapshot_age_seconds": (
                    None if coinbase_row is None
                    else decision_time - float(coinbase_row["created_at"])
                ),
                "coinbase_depth_imbalance": (
                    None if coinbase_row is None else _num(coinbase_row.get("depth_imbalance"))
                ),
            })
    finally:
        strategy.close()
        settlement.close()
        spot.close()
        coinbase.close()
    coverage = {
        "source_rows": len(source_rows),
        "examples": len(examples),
        "windows": len({row["close_time"] for row in examples}),
        "rejected": dict(sorted(rejected.items())),
        "spot_snapshot_fresh_3s_book_fresh_2s": sum(row["spot_fresh"] for row in examples),
        "coinbase_snapshot_within_5s": sum(
            row["coinbase_snapshot_age_seconds"] is not None
            and 0.0 <= row["coinbase_snapshot_age_seconds"] <= 5.0
            for row in examples
        ),
    }
    confirmation_coverage = {"30": 0, "60": 0}
    if v95_db and Path(v95_db).exists() and examples:
        v95 = sqlite3.connect(v95_db, timeout=30.0)
        v95.row_factory = sqlite3.Row
        try:
            lower = min(float(row["decision_time"]) for row in examples) + 20.0
            upper = max(float(row["decision_time"]) for row in examples) + 70.0
            predictions = [
                dict(row) for row in v95.execute(
                    "SELECT asset,close_time,created_at,entry_ask_cents FROM predictions "
                    "WHERE created_at BETWEEN ? AND ?",
                    (lower, upper),
                )
            ]
        finally:
            v95.close()
        by_window: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
        for prediction in predictions:
            canonical = round(float(prediction["close_time"]) / 900.0) * 900.0
            by_window[(str(prediction["asset"]), canonical)].append(prediction)
        for delay in (30, 60):
            for row in examples:
                candidates = by_window.get((str(row["asset"]), float(row["close_time"])), ())
                target = float(row["decision_time"]) + delay
                if candidates and min(
                    abs(float(candidate["created_at"]) - target) for candidate in candidates
                ) <= 10.0:
                    confirmation_coverage[str(delay)] += 1
    coverage["delayed_kalshi_quote_within_10s"] = confirmation_coverage
    return examples, coverage


def _candidate_functions() -> dict[str, Callable[[Mapping[str, Any]], bool]]:
    def spot_aligned(row: Mapping[str, Any]) -> bool:
        imbalance = row.get("spot_depth_imbalance")
        return bool(
            row.get("spot_fresh") and imbalance is not None
            and ((row["side"] == "YES" and float(imbalance) > 0.0)
                 or (row["side"] == "NO" and float(imbalance) < 0.0))
        )

    def aligns(value: Any, row: Mapping[str, Any]) -> bool:
        number = _num(value)
        return bool(
            number is not None and number != 0.0
            and ((row["side"] == "YES" and number > 0.0)
                 or (row["side"] == "NO" and number < 0.0))
        )

    return {
        "frozen_control": lambda row: bool(row["control"]),
        "control_margin_0_75bps": lambda row: bool(row["control"] and row["distance_into_side_bps"] >= 0.75),
        "control_margin_1bps": lambda row: bool(row["control"] and row["distance_into_side_bps"] >= 1.0),
        "control_momentum_0_25bps": lambda row: bool(row["control"] and row["side_move_bps"] >= 0.25),
        "control_margin1_momentum025": lambda row: bool(
            row["control"] and row["distance_into_side_bps"] >= 1.0
            and row["side_move_bps"] >= 0.25
        ),
        "control_stable_path": lambda row: bool(
            row["control"] and row["path_crossings"] <= 2
            and row["seconds_since_cross"] >= 30.0
        ),
        "control_non_decelerating": lambda row: bool(
            row["control"] and row["momentum_acceleration_bps"] >= 0.0
        ),
        "control_volatility_buffer_0_1": lambda row: bool(
            row["control"] and row["volatility_normalized_margin"] >= 0.10
        ),
        "control_market_confirmed_55c": lambda row: bool(
            row["control"] and row["ask"] >= 55.0
        ),
        "spot_book_confirm_v1": lambda row: bool(row["control"] and spot_aligned(row)),
        "control_spot_flow15_align": lambda row: bool(
            row["control"] and row["spot_fresh"]
            and aligns(row["spot_trade_net_notional_15s"], row)
        ),
        "control_coinbase_l2_align": lambda row: bool(
            row["control"] and row["coinbase_snapshot_age_seconds"] is not None
            and 0.0 <= row["coinbase_snapshot_age_seconds"] <= 5.0
            and aligns(row["coinbase_depth_imbalance"], row)
        ),
        "value_price_wide_v1": lambda row: bool(row["value_wide"]),
        "value_price_wide_margin1": lambda row: bool(
            row["value_wide"] and row["distance_into_side_bps"] >= 1.0
        ),
        "strong_path_wide_v1": lambda row: bool(row["strong_wide"]),
        "strong_path_wide_margin1": lambda row: bool(
            row["strong_wide"] and row["distance_into_side_bps"] >= 1.0
        ),
    }


def _group_metrics(
    rows: Sequence[Mapping[str, Any]],
    key: Callable[[Mapping[str, Any]], str],
    *,
    total_windows: int,
) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {
        label: _metrics(group, total_windows=total_windows)
        for label, group in sorted(groups.items())
    }


def audit(examples: Sequence[Mapping[str, Any]], coverage: Mapping[str, Any]) -> dict[str, Any]:
    historical_examples = [
        row
        for row in examples
        if float(row["close_time"]) <= HISTORICAL_FREEZE_CLOSE_TIME
    ]
    # Synthetic/unit datasets predate the real freeze and naturally use every row.
    if not historical_examples:
        historical_examples = list(examples)
    post_freeze_examples = [
        row
        for row in examples
        if float(row["close_time"]) > HISTORICAL_FREEZE_CLOSE_TIME
    ]
    windows = sorted({float(row["close_time"]) for row in historical_examples})
    train_end = int(len(windows) * 0.60)
    calibration_end = int(len(windows) * 0.80)
    folds = {
        "train": set(windows[:train_end]),
        "calibration": set(windows[train_end:calibration_end]),
        "test": set(windows[calibration_end:]),
    }
    candidates = _candidate_functions()
    results: dict[str, Any] = {}
    for name, predicate in candidates.items():
        selected = [row for row in historical_examples if predicate(row)]
        results[name] = {
            "all": _metrics(selected, total_windows=len(windows)),
            "folds": {
                fold: _metrics(
                    [row for row in selected if row["close_time"] in fold_windows],
                    total_windows=len(fold_windows),
                )
                for fold, fold_windows in folds.items()
            },
            "excluding_three_known_losses": _metrics(
                [
                    row for row in selected
                    if (row["asset"], float(row["close_time"])) not in KNOWN_LOSSES
                ],
                total_windows=len(windows),
            ),
        }
    control_rows = [
        row for row in historical_examples if candidates["frozen_control"](row)
    ]
    spot_rows = [
        row
        for row in historical_examples
        if candidates["spot_book_confirm_v1"](row)
    ]
    control_ids = {row["id"] for row in control_rows}
    spot_ids = {row["id"] for row in spot_rows}
    excluded = [row for row in control_rows if row["id"] in control_ids - spot_ids]

    def distance_tier(row: Mapping[str, Any]) -> str:
        value = float(row["distance_into_side_bps"])
        return "under_0.75" if value < 0.75 else ("0.75_to_1.5" if value < 1.5 else "1.5_plus")

    def volatility_tier(row: Mapping[str, Any]) -> str:
        value = float(row["path_range_bps"])
        return "under_1" if value < 1.0 else ("1_to_3" if value < 3.0 else "3_plus")

    def regime(row: Mapping[str, Any]) -> str:
        if int(row["path_crossings"]) >= 3:
            return "choppy"
        if float(row["persistence"]) >= 0.95 and int(row["path_crossings"]) <= 1:
            return "persistent"
        return "mixed"

    def transfer_cohort(row: Mapping[str, Any]) -> str:
        return "BTC" if row["asset"] == "BTC" else "NON_BTC_TRANSFER"

    def reversal_risk(row: Mapping[str, Any]) -> str:
        crossings = int(row["path_crossings"])
        acceleration = float(row["momentum_acceleration_bps"])
        since_cross = float(row["seconds_since_cross"])
        if crossings >= 3 or (acceleration < 0.0 and since_cross < 30.0):
            return "high"
        if crossings >= 1 or acceleration < 0.0:
            return "medium"
        return "low"

    def settlement_average_risk(row: Mapping[str, Any]) -> str:
        normalized = float(row["volatility_normalized_margin"])
        margin = float(row["distance_into_side_bps"])
        if margin < 0.75 or normalized < 0.05:
            return "high"
        if margin < 1.5 or normalized < 0.15:
            return "medium"
        return "low"

    def market_agreement(row: Mapping[str, Any]) -> str:
        ask = float(row["ask"])
        return "disagrees_under_50" if ask < 50.0 else (
            "weak_50_to_55" if ask < 55.0 else "confirms_55_plus"
        )

    def liquidity_tier(row: Mapping[str, Any]) -> str:
        depth = _num(row.get("kalshi_depth_contracts"))
        if depth is None:
            return "missing"
        return "under_100" if depth < 100.0 else (
            "100_to_500" if depth < 500.0 else "500_plus"
        )

    def spread_tier(row: Mapping[str, Any]) -> str:
        spread = float(row["spread"])
        return "under_0.5c" if spread < 0.5 else (
            "0.5_to_1c" if spread <= 1.0 else "over_1c"
        )

    def aggressive_flow(row: Mapping[str, Any]) -> str:
        value = _num(row.get("spot_trade_net_notional_15s"))
        if value is None or value == 0.0:
            return "missing_or_flat"
        aligned = (row["side"] == "YES" and value > 0.0) or (
            row["side"] == "NO" and value < 0.0
        )
        return "aligned" if aligned else "opposed"

    for name, predicate in candidates.items():
        selected_rows = [row for row in historical_examples if predicate(row)]
        selected_ids = {row["id"] for row in selected_rows}
        results[name]["by_transfer_cohort"] = _group_metrics(
            selected_rows,
            transfer_cohort,
            total_windows=len(windows),
        )
        results[name]["rejected_control_counterfactual"] = _metrics(
            [row for row in control_rows if row["id"] not in selected_ids],
            total_windows=len(windows),
        )

    spot_all = results["spot_book_confirm_v1"]["all"]
    promotion_eligible = bool(
        int(spot_all["n"]) >= 30
        and float(spot_all["fee_slippage_adjusted_pnl_cents_per_contract"]) > 0.0
        and spot_all["wilson_95_low"] is not None
        and spot_all["avg_fee_adjusted_break_even_rate"] is not None
        and float(spot_all["wilson_95_low"])
        > float(spot_all["avg_fee_adjusted_break_even_rate"])
    )
    return {
        "audit_version": "q15-rti-improvement-audit-v1",
        "paper_only": True,
        "as_of_close_time": max(windows) if windows else None,
        "latest_available_close_time": max(
            (float(row["close_time"]) for row in examples), default=None
        ),
        "coverage": {
            **dict(coverage),
            "historical_frozen_examples": len(historical_examples),
            "historical_frozen_windows": len(windows),
            "post_freeze_reconstructed_examples": len(post_freeze_examples),
            "post_freeze_reconstructed_windows": len(
                {row["close_time"] for row in post_freeze_examples}
            ),
        },
        "split": {
            "method": "chronological_60_20_20_grouped_by_close_time",
            "train_windows": len(folds["train"]),
            "calibration_windows": len(folds["calibration"]),
            "test_windows": len(folds["test"]),
            "test_first_close": min(folds["test"]) if folds["test"] else None,
            "test_last_close": max(folds["test"]) if folds["test"] else None,
        },
        "candidates": results,
        "selected_shadow": {
            "id": "spot_book_confirm_v1",
            "rule_version": "rti-path-spot-book-confirm-shadow-20260718-v1",
            "fee_schedule_version": "kalshi-fee-schedule-20260707",
            "rules": {
                "frozen_strict_control_required": True,
                "spot_snapshot_at_or_before_decision": True,
                "spot_snapshot_max_age_seconds": 3.0,
                "spot_book_max_age_seconds": 2.0,
                "spot_book_min_age_seconds": -3.0,
                "spot_depth_imbalance_must_align_with_rti_side": True,
            },
            "notification_eligible": False,
            "promotion_eligible": promotion_eligible,
            "promotion_review_bars": [30, 60, 150],
            "manual_promotion_only": True,
            "excluded_control_counterfactual": _metrics(excluded, total_windows=len(windows)),
        },
        "selection_integrity": {
            "historical_freeze_close_time": HISTORICAL_FREEZE_CLOSE_TIME,
            "historical_final_fold_was_seen_during_exploration": True,
            "historical_final_fold_is_truly_untouched": False,
            "thresholds_may_not_be_retuned_from_historical_final_fold": True,
            "only_genuinely_untouched_test": "prospective ledger after rule freeze",
            "historical_results_can_promote": False,
        },
        "post_freeze_reconstruction_not_promotion_evidence": {
            "reason": (
                "These rows were reconstructed after rule freeze and are not counted; "
                "only candidate verdicts frozen in the durable prospective ledger count."
            ),
            "spot_book_confirm_v1": _metrics(
                [
                    row
                    for row in post_freeze_examples
                    if candidates["spot_book_confirm_v1"](row)
                ],
                total_windows=len(
                    {row["close_time"] for row in post_freeze_examples}
                ),
            ),
        },
        "selected_breakdowns": {
            "by_asset": _group_metrics(spot_rows, lambda row: str(row["asset"]), total_windows=len(windows)),
            "by_transfer_cohort": _group_metrics(spot_rows, transfer_cohort, total_windows=len(windows)),
            "by_side": _group_metrics(spot_rows, lambda row: str(row["side"]), total_windows=len(windows)),
            "by_distance_tier": _group_metrics(spot_rows, distance_tier, total_windows=len(windows)),
            "by_volatility_tier": _group_metrics(spot_rows, volatility_tier, total_windows=len(windows)),
            "by_regime": _group_metrics(spot_rows, regime, total_windows=len(windows)),
            "by_reversal_risk": _group_metrics(spot_rows, reversal_risk, total_windows=len(windows)),
            "by_settlement_average_risk": _group_metrics(
                spot_rows, settlement_average_risk, total_windows=len(windows)
            ),
        },
        "frozen_control_signal_breakdowns": {
            "by_market_agreement": _group_metrics(
                control_rows, market_agreement, total_windows=len(windows)
            ),
            "by_kalshi_liquidity_tier": _group_metrics(
                control_rows, liquidity_tier, total_windows=len(windows)
            ),
            "by_spread_tier": _group_metrics(
                control_rows, spread_tier, total_windows=len(windows)
            ),
            "by_aggressive_spot_flow": _group_metrics(
                control_rows, aggressive_flow, total_windows=len(windows)
            ),
            "by_reversal_risk": _group_metrics(
                control_rows, reversal_risk, total_windows=len(windows)
            ),
            "by_settlement_average_risk": _group_metrics(
                control_rows, settlement_average_risk, total_windows=len(windows)
            ),
        },
        "confirmation_investigation": {
            "historical_delayed_quote_available": any(
                int(value) > 0
                for value in coverage.get("delayed_kalshi_quote_within_10s", {}).values()
            ),
            "economic_backtest_permitted": False,
            "quote_coverage": coverage.get("delayed_kalshi_quote_within_10s", {}),
            "reason": "No point-in-time Kalshi quote was stored at decision+30s or decision+60s; the 13M quote cannot be reused.",
        },
        "adversarial_review": {
            "features_use_only_rows_at_or_before_decision": True,
            "settlement_used_only_for_grading": True,
            "all_assets_in_same_close_window_share_fold": True,
            "historical_final_fold_not_misrepresented_as_untouched": True,
            "historical_final_fold_not_used_for_any_further_retuning": True,
            "known_loss_exclusion_reported": True,
            "external_snapshot_created_after_decision_rejected": True,
            "limitations": [
                f"Only {int(spot_all['n'])} selected frozen-historical rows; confidence interval does not clear break-even.",
                "The result loses its accuracy advantage when the three known losses are excluded.",
                "Coinbase L2 exact-time coverage is insufficient for a defensible candidate.",
                "No historical delayed quote exists for a valid confirmation-fill audit.",
                "The historical final fold was visible during exploration, so only prospective rows are genuinely untouched.",
            ],
        },
    }


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100.0 * float(value):.1f}%"


def markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Q15 Exact RTI 13M Improvement Audit",
        "",
        "Mode: **paper-only; frozen control unchanged; no automatic promotion**",
        "",
        "## Chronological audit",
        "",
        "The final historical fold is a chronological holdout, but it was visible during "
        "exploration and is therefore **not represented as genuinely untouched**. The durable "
        "prospective ledger is the untouched test.",
        f"Historical cutoff: `{report['selection_integrity']['historical_freeze_close_time']}`; "
        "later reconstructed settlements cannot move fold boundaries or count for promotion.",
        "",
        "| Candidate | Train | Calibration | Historical holdout | n | Freq/window | Accuracy | Wilson 95% | 10-lot net | EV/contract | Max DD/contract |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ordered = tuple(report["candidates"])
    for name in ordered:
        result = report["candidates"][name]
        def cell(fold: str) -> str:
            value = result["folds"][fold]
            return f"{value['correct']}/{value['n']} ({_pct(value['accuracy'])})"
        all_metric = result["all"]
        ev = all_metric["ev_cents_per_contract"]
        max_dd = all_metric["max_drawdown_cents_per_contract"]
        ev_cell = "n/a" if ev is None else f"{float(ev):.2f}c"
        drawdown_cell = "n/a" if max_dd is None else f"{float(max_dd):.2f}c"
        lines.append(
            f"| {name} | {cell('train')} | {cell('calibration')} | {cell('test')} | "
            f"{all_metric['n']} | {all_metric['trade_frequency_per_window']:.3f} | "
            f"{_pct(all_metric['accuracy'])} | "
            f"{_pct(all_metric['wilson_95_low'])}-{_pct(all_metric['wilson_95_high'])} | "
            f"${all_metric['ten_contract_net_pnl_dollars']:.2f} | "
            f"{ev_cell} | {drawdown_cell} |"
        )
    selected = report["candidates"]["spot_book_confirm_v1"]["all"]
    excluded = report["selected_shadow"]["excluded_control_counterfactual"]
    spot_without_losses = report["candidates"]["spot_book_confirm_v1"][
        "excluding_three_known_losses"
    ]
    control_without_losses = report["candidates"]["frozen_control"][
        "excluding_three_known_losses"
    ]
    lines.extend([
        "",
        "## Decision",
        "",
        "`spot_book_confirm_v1` is the most defensible candidate to collect prospectively "
        "because it adds independent, timestamp-valid directional book evidence to the frozen "
        "control. Its small historical result is exploratory and cannot establish improvement.",
        "",
        f"- Resolved: {selected['n']}",
        f"- Accuracy: {_pct(selected['accuracy'])}",
        f"- Wilson 95% lower bound: {_pct(selected['wilson_95_low'])}",
        f"- Average fee-adjusted break-even: {_pct(selected['avg_fee_adjusted_break_even_rate'])}",
        f"- Average fee+slippage break-even: {_pct(selected['avg_fee_slippage_adjusted_break_even_rate'])}",
        f"- Promotion eligible: **{report['selected_shadow']['promotion_eligible']}**",
        "- Independent Telegram trigger: **False** (a match is labeled in the frozen-control PAPER alert)",
        "- Review gates: 30 / 60 / 150 prospective resolved picks, manual only",
        (
            f"- Frozen-control picks rejected by this filter: {excluded['correct']}/"
            f"{excluded['n']}, 10-lot net ${excluded['ten_contract_net_pnl_dollars']:.2f}"
        ),
        (
            "- Excluding the three known losses: shadow "
            f"{spot_without_losses['correct']}/{spot_without_losses['n']} vs control "
            f"{control_without_losses['correct']}/{control_without_losses['n']}; "
            "no statistically defensible improvement survives"
        ),
        "",
        "## Audit limitations",
        "",
    ])
    lines.extend(f"- {item}" for item in report["adversarial_review"]["limitations"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default="data/q15_strategy_bots_v3.sqlite3")
    parser.add_argument("--settlement-db", default="data/q15_settlement_index_v1.sqlite3")
    parser.add_argument("--spot-db", default="data/q15_spot_depth_v1.sqlite3")
    parser.add_argument("--coinbase-db", default="data/q15_coinbase_adv_l2_v1.sqlite3")
    parser.add_argument("--v95-db", default="data/q15_v95_ledger_v1.sqlite3")
    parser.add_argument("--output-dir", default="work/rti-improvement")
    args = parser.parse_args()
    examples, coverage = reconstruct(
        strategy_db=args.strategy_db,
        settlement_db=args.settlement_db,
        spot_db=args.spot_db,
        coinbase_db=args.coinbase_db,
        v95_db=args.v95_db,
    )
    report = audit(examples, coverage)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "audit.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (output / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "examples": coverage["examples"],
        "windows": coverage["windows"],
        "selected": report["selected_shadow"],
        "audit_json": str((output / "audit.json").resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
