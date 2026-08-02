"""Cost-aware selective RTI exploration on sealed V17 development labels.

Every rule is declared in code and reported. Results are development-only and
cannot promote, notify, or trade; a chosen successor must be frozen and tested
on new prospective windows.
"""
from __future__ import annotations

from collections import defaultdict
import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.entry_economics.costs import kalshi_fee_cents
from tools import q15_rti_v17_development_runner as v17_runner


DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_RESERVATION = (
    ROOT / "reports" / "q15_rti_v17_development_runs"
    / "non_btc_transfer" / "development-reservation.json"
)
DEFAULT_RESULT = (
    ROOT / "reports" / "q15_rti_v17_development_runs"
    / "non_btc_transfer" / "development-reservation.result.json"
)
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v18_exploration"
    / "v17-development-selective-value-v1.json"
)
ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
SLIPPAGE_CENTS = 2.0
CONTRACTS = 10


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _wilson(correct: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.959963984540054
    rate = correct / count
    denominator = 1.0 + z * z / count
    centre = rate + z * z / (2.0 * count)
    margin = z * math.sqrt(
        rate * (1.0 - rate) / count + z * z / (4.0 * count * count)
    )
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def _read_rows(
    db_path: Path, reservation_path: Path, result_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reservation = v17_runner._read_sealed(reservation_path)
    result = v17_runner._read_sealed(result_path)
    v17_runner._validate_result(result, reservation)
    labels = {
        int(row["id"]): int(row["label_yes"])
        for row in result["development_label_rows"]
    }
    requested = tuple(sorted(labels))
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        for start in range(0, len(requested), 400):
            chunk = requested[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT id,asset,close_time,side,entry_ask_cents,spread_cents,"
                "depth_contracts,threshold_json FROM strategy_bot_decisions "
                f"WHERE id IN ({placeholders})"
            )
            for raw in connection.execute(query, chunk):
                row = dict(raw)
                try:
                    profile = json.loads(str(row.pop("threshold_json") or ""))
                except json.JSONDecodeError as exc:
                    raise ValueError("v18_selective_profile_invalid") from exc
                if not isinstance(profile, Mapping):
                    raise ValueError("v18_selective_profile_invalid")
                rows.append({
                    **row,
                    "profile": dict(profile),
                    "label_yes": labels[int(row["id"])],
                })
    finally:
        connection.close()
    rows.sort(key=lambda row: (float(row["close_time"]), int(row["id"])))
    if (
        {int(row["id"]) for row in rows} != set(requested)
        or len(rows) != 1440
        or any(str(row["asset"]).upper() not in ASSETS for row in rows)
    ):
        raise ValueError("v18_selective_row_binding_invalid")
    by_close: dict[float, set[str]] = defaultdict(set)
    for row in rows:
        by_close[float(row["close_time"])].add(str(row["asset"]).upper())
    if len(by_close) != 240 or any(assets != ASSETS for assets in by_close.values()):
        raise ValueError("v18_selective_close_geometry_invalid")
    return rows, result


def _side_probability(row: Mapping[str, Any]) -> float | None:
    return _num(dict(row["profile"]).get("rti_market_mid_probability"))


def _entry(row: Mapping[str, Any], side_mode: str) -> dict[str, Any] | None:
    profile = dict(row["profile"])
    rti_side = str(row.get("side") or profile.get("rti_side") or "").upper()
    rti_ask = _num(row.get("entry_ask_cents"))
    opposite_side = str(profile.get("rti_opposite_side") or "").upper()
    opposite_ask = _num(profile.get("rti_opposite_ask_cents"))
    rti_probability = _side_probability(row)
    if (
        rti_side not in {"YES", "NO"}
        or opposite_side not in {"YES", "NO"}
        or opposite_side == rti_side
        or rti_ask is None
        or opposite_ask is None
        or rti_probability is None
    ):
        return None
    if side_mode == "RTI":
        side, ask, probability = rti_side, rti_ask, rti_probability
    elif side_mode == "OPPOSITE":
        side, ask, probability = opposite_side, opposite_ask, 1.0 - rti_probability
    elif side_mode == "MARKET":
        if rti_probability >= 0.5:
            side, ask, probability = rti_side, rti_ask, rti_probability
        else:
            side, ask, probability = opposite_side, opposite_ask, 1.0 - rti_probability
    else:
        raise ValueError("v18_selective_side_mode_invalid")
    fill = min(99.0, float(ask) + SLIPPAGE_CENTS)
    fee = float(kalshi_fee_cents(fill, contracts=CONTRACTS, rate=0.07, ceil=True))
    outcome = "YES" if int(row["label_yes"]) == 1 else "NO"
    correct = side == outcome
    pnl_per_contract = (100.0 - fill - fee) if correct else (-fill - fee)
    return {
        "side": side,
        "ask_cents": float(ask),
        "fill_cents": fill,
        "fee_cents_per_contract": fee,
        "market_probability": probability,
        "break_even_probability": (fill + fee) / 100.0,
        "correct": correct,
        "pnl_cents": pnl_per_contract * CONTRACTS,
    }


def _strict(row: Mapping[str, Any]) -> bool:
    return dict(row["profile"]).get("passed") is True


def _challenger(row: Mapping[str, Any], name: str) -> bool:
    value = dict(dict(row["profile"]).get("challengers") or {}).get(name)
    return isinstance(value, Mapping) and value.get("accepted") is True


def _path_base(row: Mapping[str, Any]) -> bool:
    profile = dict(row["profile"])
    return bool(
        profile.get("rti_path_complete") is True
        and _num(profile.get("rti_path_persistence")) is not None
        and float(profile["rti_path_persistence"]) >= 0.8
        and _num(profile.get("rti_side_move_bps")) is not None
        and float(profile["rti_side_move_bps"]) >= 0.0
        and _num(row.get("spread_cents")) is not None
        and float(row["spread_cents"]) <= 1.5
    )


def rule_manifest() -> tuple[dict[str, Any], ...]:
    rules: list[dict[str, Any]] = [{
        "name": "STRICT_CONTROL", "side_mode": "RTI",
        "predicate": _strict,
    }]
    for threshold in (40.0, 50.0, 58.0, 62.0):
        rules.append({
            "name": f"STRICT_ASK_MAX_{int(threshold)}", "side_mode": "RTI",
            "predicate": lambda row, value=threshold: bool(
                _strict(row) and float(row["entry_ask_cents"]) <= value
            ),
        })
    for threshold in (0.55, 0.60, 0.65):
        rules.append({
            "name": f"STRICT_MARKET_SIDE_PROB_GE_{int(threshold * 100)}",
            "side_mode": "RTI",
            "predicate": lambda row, value=threshold: bool(
                _strict(row)
                and _side_probability(row) is not None
                and float(_side_probability(row)) >= value
            ),
        })
    rules.extend((
        {
            "name": "STRICT_ZERO_CROSSINGS", "side_mode": "RTI",
            "predicate": lambda row: bool(
                _strict(row)
                and int(dict(row["profile"]).get("rti_path_strike_crossings") or 0) == 0
            ),
        },
        {
            "name": "STRICT_LOW_REVERSAL_RISK", "side_mode": "RTI",
            "predicate": lambda row: bool(
                _strict(row)
                and dict(row["profile"]).get("rti_reversal_risk_class") == "low"
            ),
        },
        {
            "name": "STRICT_LOW_SETTLEMENT_AVERAGE_RISK", "side_mode": "RTI",
            "predicate": lambda row: bool(
                _strict(row)
                and dict(row["profile"]).get("rti_settlement_average_risk_class") == "low"
            ),
        },
        {
            "name": "STRICT_BOTH_RISKS_LOW", "side_mode": "RTI",
            "predicate": lambda row: bool(
                _strict(row)
                and dict(row["profile"]).get("rti_reversal_risk_class") == "low"
                and dict(row["profile"]).get("rti_settlement_average_risk_class") == "low"
            ),
        },
        {
            "name": "IMPULSE_STRENGTH_V1", "side_mode": "RTI",
            "predicate": lambda row: _challenger(row, "impulse_strength_v1"),
        },
        {
            "name": "COUNTERTREND_VALUE_V1", "side_mode": "OPPOSITE",
            "predicate": lambda row: _challenger(row, "rti_countertrend_value_v1"),
        },
        {
            "name": "STRONG_PATH_WIDE_V1", "side_mode": "RTI",
            "predicate": lambda row: _challenger(row, "strong_path_wide_v1"),
        },
        {
            "name": "VALUE_PRICE_WIDE_V1", "side_mode": "RTI",
            "predicate": lambda row: _challenger(row, "value_price_wide_v1"),
        },
    ))
    for threshold in (0.55, 0.60, 0.65, 0.70):
        rules.append({
            "name": f"FRESH_MARKET_SIDE_PROB_GE_{int(threshold * 100)}_ASK_MAX_62",
            "side_mode": "MARKET",
            "predicate": lambda row, value=threshold: bool(
                _path_base(row)
                and (_entry(row, "MARKET") or {}).get("market_probability", 0.0) >= value
                and (_entry(row, "MARKET") or {}).get("ask_cents", 101.0) <= 62.0
            ),
        })
    for threshold in (0.0, 3.0, 5.0):
        rules.append({
            "name": f"FRESH_MARKET_SIDE_EDGE_GE_{int(threshold)}C",
            "side_mode": "MARKET",
            "predicate": lambda row, value=threshold: bool(
                _path_base(row)
                and (
                    ((_entry(row, "MARKET") or {}).get("market_probability", 0.0) * 100.0)
                    - ((_entry(row, "MARKET") or {}).get("fill_cents", 101.0))
                    - ((_entry(row, "MARKET") or {}).get("fee_cents_per_contract", 100.0))
                ) >= value
            ),
        })
    return tuple(rules)


def _metrics(picks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(picks)
    correct = sum(bool(pick["entry"]["correct"]) for pick in picks)
    low, high = _wilson(correct, count)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pick in picks:
        cumulative += float(pick["entry"]["pnl_cents"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    pnl = sum(float(pick["entry"]["pnl_cents"]) for pick in picks)
    break_even = (
        _mean([float(pick["entry"]["break_even_probability"]) for pick in picks])
        if picks else None
    )
    return {
        "trades": count,
        "correct": correct,
        "accuracy": correct / count if count else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "average_break_even_probability": break_even,
        "fee_slippage_adjusted_pnl_cents_10_contracts": pnl,
        "fee_slippage_adjusted_pnl_dollars_10_contracts": pnl / 100.0,
        "ev_cents_per_trade_10_contracts": pnl / count if count else None,
        "maximum_drawdown_cents_10_contracts": max_drawdown,
        "yes_trades": sum(pick["entry"]["side"] == "YES" for pick in picks),
        "no_trades": sum(pick["entry"]["side"] == "NO" for pick in picks),
        "close_windows": len({float(pick["close_time"]) for pick in picks}),
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def build_report(
    *, db_path: Path, reservation_path: Path, result_path: Path,
) -> dict[str, Any]:
    rows, result = _read_rows(db_path, reservation_path, result_path)
    windows = tuple(sorted({float(row["close_time"]) for row in rows}))
    train_end = int(len(windows) * 0.6)
    validation_end = int(len(windows) * 0.8)
    split_times = {
        "train_older_60pct": set(windows[:train_end]),
        "validation_next_20pct": set(windows[train_end:validation_end]),
        "test_newest_20pct": set(windows[validation_end:]),
    }
    reports = []
    for rule in rule_manifest():
        picks = []
        for row in rows:
            if not bool(rule["predicate"](row)):
                continue
            entry = _entry(row, str(rule["side_mode"]))
            if entry is None:
                continue
            picks.append({
                "id": int(row["id"]),
                "asset": str(row["asset"]).upper(),
                "close_time": float(row["close_time"]),
                "entry": entry,
            })
        splits = {
            name: _metrics([
                pick for pick in picks if float(pick["close_time"]) in times
            ])
            for name, times in split_times.items()
        }
        overall = _metrics(picks)
        robust = bool(
            overall["trades"] >= 30
            and overall["fee_slippage_adjusted_pnl_cents_10_contracts"] > 0.0
            and overall["wilson_95_low"] is not None
            and overall["average_break_even_probability"] is not None
            and overall["wilson_95_low"] > overall["average_break_even_probability"]
            and all(splits[name]["trades"] >= 5 for name in split_times)
            and all(
                splits[name]["fee_slippage_adjusted_pnl_cents_10_contracts"] > 0.0
                for name in ("validation_next_20pct", "test_newest_20pct")
            )
        )
        reports.append({
            "rule": str(rule["name"]),
            "side_mode": str(rule["side_mode"]),
            "overall": overall,
            "splits": splits,
            "exploratory_robustness_screen": robust,
        })
    ranked = sorted(reports, key=lambda report: (
        -float(report["overall"]["fee_slippage_adjusted_pnl_cents_10_contracts"]),
        -int(report["overall"]["trades"]),
        str(report["rule"]),
    ))
    robust = [report for report in ranked if report["exploratory_robustness_screen"]]
    return {
        "report_version": "q15-rti-v18-selective-development-exploration-v1",
        "source_v17_result_state_sha256": result["state_sha256"],
        "source_population": "ALREADY_OPEN_V17_DEVELOPMENT_ONLY",
        "source_rows": len(rows),
        "source_close_windows": len(windows),
        "candidate_rules_use_v17_development_labels": True,
        "independent_confirmation": False,
        "candidate_grid_fully_reported": True,
        "official_kalshi_fee_formula": True,
        "slippage_cents_per_contract": SLIPPAGE_CENTS,
        "sim_contracts": CONTRACTS,
        "future_v18_prospective_confirmation_required": True,
        "future_v17_calibration_allowed": False,
        "btc_labels_read": False,
        "new_population_outcomes_read": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "ranked_rules": ranked,
        "rules_passing_exploratory_robustness_screen": [
            report["rule"] for report in robust
        ],
        "best_exploratory_rule": robust[0]["rule"] if robust else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--result", default=str(DEFAULT_RESULT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = build_report(
        db_path=Path(args.db),
        reservation_path=Path(args.reservation),
        result_path=Path(args.result),
    )
    if args.write:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    print(json.dumps({
        "report_version": report["report_version"],
        "best_exploratory_rule": report["best_exploratory_rule"],
        "rules_passing_exploratory_robustness_screen": report[
            "rules_passing_exploratory_robustness_screen"
        ],
        "ranked_rules": [{
            "rule": item["rule"],
            "trades": item["overall"]["trades"],
            "accuracy": item["overall"]["accuracy"],
            "wilson_95_low": item["overall"]["wilson_95_low"],
            "break_even": item["overall"]["average_break_even_probability"],
            "pnl_dollars": item["overall"]["fee_slippage_adjusted_pnl_dollars_10_contracts"],
            "validation_pnl_dollars": item["splits"]["validation_next_20pct"]["fee_slippage_adjusted_pnl_dollars_10_contracts"],
            "test_pnl_dollars": item["splits"]["test_newest_20pct"]["fee_slippage_adjusted_pnl_dollars_10_contracts"],
            "robust": item["exploratory_robustness_screen"],
        } for item in report["ranked_rules"]],
        "notification_eligible": False,
        "real_trading_allowed": False,
    }, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
