"""Audit frozen-vs-forward exact RTI performance without retuning on test.

This report is descriptive.  The post-freeze period has been observed and may
diagnose failure, but it may not promote a new threshold or be relabeled as an
untouched test.  Only genuine exact rows with coherent timestamps and canonical
fee/slippage economics are admitted.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Callable, Mapping, Sequence

try:
    from tools.q15_rti_improvement_audit import (
        HISTORICAL_FREEZE_CLOSE_TIME,
        _json,
        _metrics,
        _net_pnl_per_contract,
        _num,
    )
except ModuleNotFoundError:  # direct ``python tools/...py`` execution
    from q15_rti_improvement_audit import (  # type: ignore[no-redef]
        HISTORICAL_FREEZE_CLOSE_TIME,
        _json,
        _metrics,
        _net_pnl_per_contract,
        _num,
    )


def _fisher_two_sided(a: int, b: int, c: int, d: int) -> float | None:
    """Two-sided Fisher exact p-value for a 2x2 table."""
    if min(a, b, c, d) < 0 or a + b + c + d <= 0:
        return None
    row_one = a + b
    successes = a + c
    total = row_one + c + d
    denominator = math.comb(total, row_one)

    def probability(x: int) -> float:
        return (
            math.comb(successes, x)
            * math.comb(total - successes, row_one - x)
            / denominator
        )

    lower = max(0, row_one - (total - successes))
    upper = min(row_one, successes)
    observed = probability(a)
    return min(
        1.0,
        sum(
            probability(x)
            for x in range(lower, upper + 1)
            if probability(x) <= observed + 1e-15
        ),
    )


def _valid_exact_row(
    source: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    profile = _json(source.get("threshold_json"))
    if str(source.get("record_kind") or "") != "RTI_PATH_13M_PROSPECTIVE_EXACT":
        return None, "not_exact_record_kind"
    if str(profile.get("capture_mode") or "") != "kalshi_ws_exact_13m":
        return None, "not_exact_capture_mode"
    close_time = _num(source.get("close_time"))
    quote_at = _num(profile.get("quote_captured_at"))
    timing_offset = _num(profile.get("rti_timing_offset_s"))
    evaluation_delay = _num(profile.get("rti_path_evaluation_delay_s"))
    quote_age = _num(profile.get("quote_age_seconds"))
    if close_time is None or quote_at is None or timing_offset is None:
        return None, "capture_timestamp_missing"
    decision_time = close_time - 780.0
    if abs((quote_at - decision_time) - timing_offset) > 0.01:
        return None, "capture_offset_incoherent"
    if not 0.0 <= timing_offset <= 2.0:
        return None, "capture_not_exact"
    if evaluation_delay is None or not 0.0 <= evaluation_delay <= 2.0:
        return None, "evaluation_not_exact"
    if quote_age is None or not 0.0 <= quote_age <= 2.0:
        return None, "quote_stale"
    if not bool(profile.get("rti_path_complete")):
        return None, "path_incomplete"
    if int(_num(profile.get("rti_path_count")) or 0) != 61:
        return None, "path_count_not_61"
    if int(_num(profile.get("rti_path_expected_count")) or 0) != 61:
        return None, "path_expected_count_not_61"
    side = str(source.get("side") or "").upper()
    official = str(source.get("official_result") or "").upper()
    correct = int(source.get("correct") or 0)
    if side not in {"YES", "NO"} or official not in {"YES", "NO"}:
        return None, "result_missing"
    if correct != int(side == official):
        return None, "grading_incoherent"
    ask = _num(source.get("entry_ask_cents"))
    if ask is None:
        return None, "ask_missing"
    canonical = float(_net_pnl_per_contract(ask, bool(correct)))
    stored = _num(source.get("hypothetical_pnl_cents"))
    # Legacy durable rows rounded simulated P/L to one decimal cent.  Recompute
    # canonical economics here, but tolerate only that historical precision.
    if stored is None or abs(stored - canonical) > 0.11:
        return None, "economics_incoherent"
    return {
        "id": int(source["id"]),
        "close_time": close_time,
        "asset": str(source.get("asset") or "").upper(),
        "side": side,
        "ask": ask,
        "spread": _num(source.get("spread_cents")),
        "correct": correct,
        "pnl": canonical,
        "profile": profile,
    }, None


def _tier(value: float | None, cuts: Sequence[float], labels: Sequence[str]) -> str:
    if value is None:
        return "missing"
    for cut, label in zip(cuts, labels):
        if value < cut:
            return label
    return labels[-1]


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


def _feature_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "rti_signed_distance_bps",
        "rti_side_move_bps",
        "rti_path_acceleration_bps",
        "rti_path_second_half_side_move_bps",
        "rti_path_range_bps",
        "rti_path_realized_volatility_bps",
        "rti_path_trend_efficiency",
        "rti_distance_to_remaining_volatility",
        "rti_path_strike_crossings",
        "spot_depth_imbalance",
    )
    result: dict[str, Any] = {}
    for field in fields:
        values = [
            value
            for row in rows
            for value in [_num(row["profile"].get(field))]
            if value is not None
        ]
        result[field] = {
            "n": len(values),
            "median": None if not values else median(values),
        }
    asks = [float(row["ask"]) for row in rows]
    result["entry_ask_cents"] = {
        "n": len(asks),
        "median": None if not asks else median(asks),
    }
    return result


def audit(
    *,
    strategy_db: str,
    freeze_close_time: float = HISTORICAL_FREEZE_CLOSE_TIME,
) -> dict[str, Any]:
    conn = sqlite3.connect(strategy_db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rejected: Counter[str] = Counter()
    valid: list[dict[str, Any]] = []
    try:
        source_rows = conn.execute(
            "SELECT * FROM strategy_bot_decisions "
            "WHERE bot_name='rti_path_13m' "
            "AND source_system='rti_path_13m' AND interval='13M' "
            "AND official_result IN ('YES','NO') ORDER BY close_time,id"
        ).fetchall()
        for raw in source_rows:
            row, reason = _valid_exact_row(dict(raw))
            if row is None:
                rejected[str(reason or "unknown")] += 1
            else:
                valid.append(row)
    finally:
        conn.close()

    accepted = [
        row for row in valid
        if bool(row["profile"].get("passed"))
    ]
    frozen = [row for row in accepted if row["close_time"] <= freeze_close_time]
    forward = [row for row in accepted if row["close_time"] > freeze_close_time]
    valid_frozen = [row for row in valid if row["close_time"] <= freeze_close_time]
    valid_forward = [row for row in valid if row["close_time"] > freeze_close_time]
    frozen_windows = len({row["close_time"] for row in valid_frozen})
    forward_windows = len({row["close_time"] for row in valid_forward})

    frozen_correct = sum(int(row["correct"]) for row in frozen)
    forward_correct = sum(int(row["correct"]) for row in forward)
    p_value = _fisher_two_sided(
        frozen_correct,
        len(frozen) - frozen_correct,
        forward_correct,
        len(forward) - forward_correct,
    )

    def profile_num(row: Mapping[str, Any], key: str) -> float | None:
        return _num(row["profile"].get(key))

    def spot_alignment(row: Mapping[str, Any]) -> str:
        value = profile_num(row, "spot_depth_imbalance")
        if value is None:
            return "missing"
        aligned = (row["side"] == "YES" and value >= 0.0) or (
            row["side"] == "NO" and value <= 0.0
        )
        return "aligned" if aligned else "opposed"

    breakdowns: dict[str, Callable[[Mapping[str, Any]], str]] = {
        "by_asset": lambda row: str(row["asset"]),
        "by_transfer_cohort": lambda row: (
            "BTC" if row["asset"] == "BTC" else "NON_BTC_TRANSFER"
        ),
        "by_side": lambda row: str(row["side"]),
        "by_market_price": lambda row: _tier(
            float(row["ask"]), (50.0, 55.0, math.inf),
            ("under_50c", "50_to_54c", "55c_plus"),
        ),
        "by_distance": lambda row: _tier(
            profile_num(row, "rti_signed_distance_bps"),
            (1.0, 3.0, math.inf),
            ("under_1bps", "1_to_3bps", "3bps_plus"),
        ),
        "by_momentum": lambda row: _tier(
            profile_num(row, "rti_side_move_bps"),
            (0.25, 1.0, math.inf),
            ("under_0_25bps", "0_25_to_1bps", "1bps_plus"),
        ),
        "by_acceleration": lambda row: (
            "missing" if profile_num(row, "rti_path_acceleration_bps") is None
            else "decelerating"
            if float(profile_num(row, "rti_path_acceleration_bps")) < 0.0
            else "nonnegative"
        ),
        "by_second_half": lambda row: (
            "missing"
            if profile_num(row, "rti_path_second_half_side_move_bps") is None
            else "fading"
            if float(profile_num(row, "rti_path_second_half_side_move_bps")) < 0.0
            else "continuing"
        ),
        "by_spot_alignment": spot_alignment,
    }

    periods = {
        "frozen_historical": (frozen, frozen_windows),
        "post_freeze_forward": (forward, forward_windows),
    }
    period_reports = {}
    for period, (rows, windows) in periods.items():
        period_reports[period] = {
            "overall": _metrics(rows, total_windows=windows),
            "enhanced_path_feature_coverage": {
                "rows_with_signed_distance": sum(
                    profile_num(row, "rti_signed_distance_bps") is not None
                    for row in rows
                ),
                "rows": len(rows),
            },
            "correct_feature_medians": _feature_summary(
                [row for row in rows if row["correct"]]
            ),
            "wrong_feature_medians": _feature_summary(
                [row for row in rows if not row["correct"]]
            ),
            "breakdowns": {
                name: _group_metrics(rows, key, total_windows=windows)
                for name, key in breakdowns.items()
            },
        }

    return {
        "audit_version": "q15-rti-forward-drift-audit-v1",
        "paper_only": True,
        "generated_from_durable_ledger": True,
        "freeze_close_time": freeze_close_time,
        "integrity": {
            "source_rows": len(source_rows),
            "valid_exact_rows": len(valid),
            "rejected_rows": sum(rejected.values()),
            "rejection_reasons": dict(rejected.most_common()),
            "timestamp_alignment_required": True,
            "canonical_fee_slippage_economics_required": True,
            "same_close_assets_kept_in_same_period": True,
        },
        "periods": period_reports,
        "accuracy_change_test": {
            "method": "two_sided_fisher_exact",
            "frozen_correct": frozen_correct,
            "frozen_n": len(frozen),
            "forward_correct": forward_correct,
            "forward_n": len(forward),
            "p_value": p_value,
            "statistically_distinguishable_at_5pct": (
                None if p_value is None else p_value < 0.05
            ),
        },
        "interpretation_guardrails": {
            "post_freeze_period_has_been_seen": True,
            "post_freeze_period_is_no_longer_untouched_for_new_rules": True,
            "diagnosis_can_promote_new_threshold": False,
            "historical_7_of_10_established_stable_edge": False,
            "next_valid_test": "future durable prospective rows after a new rule freeze",
        },
    }


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100.0 * float(value):.1f}%"


def _fmt(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{float(value):.2f}{suffix}"


def render_markdown(report: Mapping[str, Any]) -> str:
    periods = report["periods"]
    forward = periods["post_freeze_forward"]
    high_momentum = forward["breakdowns"]["by_momentum"].get(
        "1bps_plus", {"correct": 0, "n": 0, "ten_contract_net_pnl_dollars": 0.0}
    )
    spot_aligned = forward["breakdowns"]["by_spot_alignment"].get(
        "aligned", {"correct": 0, "n": 0, "ten_contract_net_pnl_dollars": 0.0}
    )
    lines = [
        "# Q15 RTI Frozen-vs-Forward Drift Audit",
        "",
        "Mode: **paper-only; descriptive; no threshold promotion**",
        "",
        "| Period | n | Accuracy | Wilson 95% | 10-lot net | EV/contract | Trades/window |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("frozen_historical", "post_freeze_forward"):
        metrics = periods[name]["overall"]
        lines.append(
            f"| {name} | {metrics['n']} | {_pct(metrics['accuracy'])} | "
            f"{_pct(metrics['wilson_95_low'])}-{_pct(metrics['wilson_95_high'])} | "
            f"${_fmt(metrics['ten_contract_net_pnl_dollars'])} | "
            f"{_fmt(metrics['ev_cents_per_contract'], 'c')} | "
            f"{metrics['trade_frequency_per_window']:.3f} |"
        )
    change = report["accuracy_change_test"]
    lines.extend([
        "",
        "## Honest conclusion",
        "",
        f"The frozen sample was {change['frozen_correct']}/{change['frozen_n']}; "
        f"the forward sample is {change['forward_correct']}/{change['forward_n']}. "
        f"Two-sided Fisher exact p={change['p_value']:.4f}.  The large observed "
        "drop is economically real, but the original sample was too small to "
        "prove that a stable edge existed or to identify a statistically secure "
        "regime break.",
        "",
        "The forward period has now been inspected.  It can diagnose failure but "
        "cannot be reused as an untouched test or promote a newly selected filter. "
        "Any new rule must freeze first and earn evidence on later durable rows.",
        "",
        "## Forward diagnostics (not selection evidence)",
        "",
        (
            "- >=1 bps 61-second momentum: "
            f"{high_momentum['correct']}/{high_momentum['n']}, "
            f"${high_momentum['ten_contract_net_pnl_dollars']:.2f}."
        ),
        (
            "- Fresh spot-book aligned: "
            f"{spot_aligned['correct']}/{spot_aligned['n']}, "
            f"${spot_aligned['ten_contract_net_pnl_dollars']:.2f}."
        ),
        "- BTC and non-BTC transfer cohorts are both below fee+slippage break-even.",
        (
            "- Enhanced path features were unavailable for the original frozen "
            f"sample ({periods['frozen_historical']['enhanced_path_feature_coverage']['rows_with_signed_distance']}/"
            f"{periods['frozen_historical']['enhanced_path_feature_coverage']['rows']} rows), "
            "so their apparent forward relationships cannot be called a measured regime change."
        ),
        "",
        "## Integrity",
        "",
        f"- Valid exact rows: {report['integrity']['valid_exact_rows']}",
        f"- Rejected rows: {report['integrity']['rejected_rows']}",
        "- Exact timestamp, complete 61-second path, fresh quote, coherent grading, "
        "and canonical Kalshi fee + 2c slippage checks are mandatory.",
        "- All assets sharing a close remain in the same frozen/forward period.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy-db", default="data/q15_strategy_bots_v3.sqlite3"
    )
    parser.add_argument(
        "--freeze-close-time", type=float,
        default=HISTORICAL_FREEZE_CLOSE_TIME,
    )
    parser.add_argument(
        "--output-dir", default="work/rti-forward-drift"
    )
    args = parser.parse_args()
    report = audit(
        strategy_db=args.strategy_db,
        freeze_close_time=args.freeze_close_time,
    )
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "audit.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    print(json.dumps({
        "json": str(output / "audit.json"),
        "markdown": str(output / "audit.md"),
        "forward": report["periods"]["post_freeze_forward"]["overall"],
    }, indent=2))


if __name__ == "__main__":
    main()
