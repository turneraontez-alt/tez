"""Reproducible pre-V18 audit of the fresh-quote RTI confirmation ladder.

This is development evidence only.  The query is hard-bounded before V18's
prospective boundary so the audit cannot read any V18 outcome.  Every delayed
entry is charged at its newly captured ask plus two cents of slippage and the
official Kalshi general fee for a ten-contract order.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.entry_economics.costs import kalshi_fee_cents
from q15_upgrade.strategy_bots.rules import (
    BOT_RTI_PATH_13M,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
)


DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v19_exploration"
    / "pre-v18-delayed-confirmation-development-v1.json"
)
V18_PROSPECTIVE_AFTER_CLOSE_TIME = 1785573900.0
NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
CONTRACTS = 10
SLIPPAGE_CENTS = 2.0


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _profile(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("v19_invalid_threshold_json") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v19_invalid_threshold_json")
    return dict(value)


def _challenger_accepted(row: Mapping[str, Any], challenger_id: str) -> bool:
    raw = dict(dict(row["profile"]).get("challengers") or {}).get(challenger_id)
    return isinstance(raw, Mapping) and raw.get("accepted") is True


def _flag(row: Mapping[str, Any], key: str) -> bool:
    value = dict(row["profile"]).get(key)
    return value is True or value == 1


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


def _read_rows(db_path: Path) -> list[dict[str, Any]]:
    query = (
        "SELECT id,created_at,decision_status,asset,side,interval,ticker,"
        "close_time,entry_ask_cents,spread_cents,depth_contracts,"
        "official_result,threshold_json FROM strategy_bot_decisions "
        "WHERE bot_name=? AND interval IN ('13M','12M30S','12M','11M30S') "
        "AND close_time<=? ORDER BY close_time,id"
    )
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = []
        for raw in connection.execute(
            query, (BOT_RTI_PATH_13M, V18_PROSPECTIVE_AFTER_CLOSE_TIME),
        ):
            row = dict(raw)
            row["profile"] = _profile(row.pop("threshold_json"))
            rows.append(row)
        return rows
    finally:
        connection.close()


def _build_pairs(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, dict[str, Any]]], dict[str, int]]:
    parents = {
        int(row["id"]): dict(row)
        for row in rows
        if str(row.get("interval") or "").upper() == "13M"
        and str(row.get("asset") or "").upper() in NON_BTC_ASSETS
        and row.get("decision_status") == "ACCEPTED"
    }
    stages: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    diagnostics: dict[str, int] = defaultdict(int)
    challenger_by_interval = {
        "12M30S": RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
        "12M": RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
        "11M30S": RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
    }
    for raw in rows:
        row = dict(raw)
        interval = str(row.get("interval") or "").upper()
        challenger_id = challenger_by_interval.get(interval)
        if challenger_id is None:
            continue
        profile = dict(row["profile"])
        if not _flag(row, "rti_confirm_original_strict_accepted"):
            diagnostics["original_not_strict"] += 1
            continue
        parent_id = int(_num(profile.get("rti_confirm_original_row_id")) or 0)
        parent = parents.get(parent_id)
        if parent is None:
            diagnostics["missing_parent"] += 1
            continue
        if (
            parent.get("ticker") != row.get("ticker")
            or parent.get("asset") != row.get("asset")
            or _num(parent.get("close_time")) != _num(row.get("close_time"))
        ):
            diagnostics["identity_mismatch"] += 1
            continue
        if interval in stages[parent_id]:
            diagnostics["duplicate_stage"] += 1
            continue
        if not isinstance(
            dict(profile.get("challengers") or {}).get(challenger_id), Mapping,
        ):
            diagnostics["missing_challenger_evaluation"] += 1
            continue
        row["challenger_id"] = challenger_id
        row["accepted"] = _challenger_accepted(row, challenger_id)
        stages[parent_id][interval] = row
    return parents, dict(stages), dict(sorted(diagnostics.items()))


def _score(row: Mapping[str, Any]) -> dict[str, Any] | None:
    official = str(row.get("official_result") or "").upper()
    side = str(row.get("side") or "").upper()
    ask = _num(row.get("entry_ask_cents"))
    if official not in {"YES", "NO"} or side not in {"YES", "NO"} or ask is None:
        return None
    fill = min(99.0, ask + SLIPPAGE_CENTS)
    fee = kalshi_fee_cents(fill, contracts=CONTRACTS, rate=0.07, ceil=True)
    correct = side == official
    pnl_per_contract = 100.0 - fill - fee if correct else -fill - fee
    return {
        "correct": correct,
        "fill_cents": fill,
        "fee_cents_per_contract": fee,
        "break_even_rate": (fill + fee) / 100.0,
        "pnl_cents": pnl_per_contract * CONTRACTS,
    }


def _aggregate(entries: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]) -> dict[str, Any]:
    scored = []
    for parent, entry in entries:
        result = _score(entry)
        if result is not None:
            scored.append((parent, entry, result))
    correct = sum(int(result["correct"]) for _, _, result in scored)
    count = len(scored)
    low, high = _wilson(correct, count)
    cumulative = peak = max_drawdown = 0.0
    for _, _, result in sorted(
        scored,
        key=lambda item: (float(item[0]["close_time"]), int(item[0]["id"])),
    ):
        cumulative += float(result["pnl_cents"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    by_asset: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    by_side: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = defaultdict(list)
    for parent, entry, _ in scored:
        by_asset[str(parent["asset"]).upper()].append((parent, entry))
        by_side[str(entry["side"]).upper()].append((parent, entry))
    summary = {
        "resolved": count,
        "correct": correct,
        "accuracy": None if not count else correct / count,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "average_fee_slippage_break_even_rate": (
            None if not count else sum(
                float(result["break_even_rate"]) for _, _, result in scored
            ) / count
        ),
        "ten_contract_net_pnl_dollars": cumulative / 100.0,
        "ten_contract_ev_per_trade_dollars": None if not count else cumulative / count / 100.0,
        "ten_contract_max_drawdown_dollars": max_drawdown / 100.0,
    }
    if count:
        summary["by_asset"] = {
            key: _aggregate_shallow(value) for key, value in sorted(by_asset.items())
        }
        summary["by_side"] = {
            key: _aggregate_shallow(value) for key, value in sorted(by_side.items())
        }
    return summary


def _aggregate_shallow(
    entries: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    results = [result for _, entry in entries if (result := _score(entry)) is not None]
    count = len(results)
    correct = sum(int(result["correct"]) for result in results)
    low, high = _wilson(correct, count)
    pnl = sum(float(result["pnl_cents"]) for result in results)
    return {
        "resolved": count,
        "correct": correct,
        "accuracy": None if not count else correct / count,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "ten_contract_net_pnl_dollars": pnl / 100.0,
    }


def _low_reversal(parent: Mapping[str, Any]) -> bool:
    return str(dict(parent["profile"]).get("rti_reversal_risk_class") or "").lower() == "low"


def _evaluate_rule(
    name: str,
    parents: Mapping[int, Mapping[str, Any]],
    stages: Mapping[int, Mapping[str, Mapping[str, Any]]],
    required_intervals: Sequence[str],
    entry_interval: str,
    require_low_reversal: bool,
) -> dict[str, Any]:
    universe: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    accepted: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    rejected: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for parent_id, parent in parents.items():
        stage_set = stages.get(parent_id, {})
        if any(interval not in stage_set for interval in required_intervals):
            continue
        universe.append((parent, parent))
        passed = (not require_low_reversal or _low_reversal(parent)) and all(
            bool(stage_set[interval].get("accepted")) for interval in required_intervals
        )
        if passed:
            accepted.append((parent, stage_set[entry_interval]))
        else:
            rejected.append((parent, parent))
    control = _aggregate(universe)
    candidate = _aggregate(accepted)
    rejected_counterfactual = _aggregate(rejected)
    control_pnl = float(control["ten_contract_net_pnl_dollars"])
    candidate_pnl = float(candidate["ten_contract_net_pnl_dollars"])
    return {
        "name": name,
        "required_intervals": list(required_intervals),
        "entry_interval": entry_interval,
        "new_quote_required": True,
        "require_parent_low_reversal": require_low_reversal,
        "matched_parent_count": len(universe),
        "candidate_count": len(accepted),
        "rejected_count": len(rejected),
        "qualification_rate": None if not universe else len(accepted) / len(universe),
        "matched_strict_control": control,
        "candidate": candidate,
        "rejected_parent_counterfactual": rejected_counterfactual,
        "candidate_incremental_pnl_vs_execute_every_matched_parent_dollars": (
            candidate_pnl - control_pnl
        ),
    }


def build_report(db_path: Path) -> dict[str, Any]:
    rows = _read_rows(db_path)
    parents, stages, lineage = _build_pairs(rows)
    rules = [
        _evaluate_rule(
            "FRESH_30_CONTINUATION", parents, stages,
            ("12M30S",), "12M30S", False,
        ),
        _evaluate_rule(
            "FRESH_60_CONTINUATION", parents, stages,
            ("12M",), "12M", False,
        ),
        _evaluate_rule(
            "LOW_REVERSAL_FRESH_30", parents, stages,
            ("12M30S",), "12M30S", True,
        ),
        _evaluate_rule(
            "LOW_REVERSAL_FRESH_60", parents, stages,
            ("12M",), "12M", True,
        ),
        _evaluate_rule(
            "LOW_REVERSAL_FRESH_30_AND_60", parents, stages,
            ("12M30S", "12M"), "12M", True,
        ),
    ]
    evidence_ids = sorted(
        (int(row["id"]), str(row["interval"]), float(row["close_time"]))
        for row in rows
    )
    evidence_hash = hashlib.sha256(
        json.dumps(evidence_ids, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "audit_id": "q15-rti-v19-delayed-development-audit-v1",
        "status": "DEVELOPMENT_ONLY_NO_PROMOTION",
        "cohort": "NON_BTC_TRANSFER",
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_promotion": False,
        "historical_credit_allowed": False,
        "v18_outcomes_read": False,
        "maximum_label_close_time": V18_PROSPECTIVE_AFTER_CLOSE_TIME,
        "v18_prospective_after_close_time": V18_PROSPECTIVE_AFTER_CLOSE_TIME,
        "execution_model": {
            "contracts": CONTRACTS,
            "slippage_cents_per_contract": SLIPPAGE_CENTS,
            "fee": "official Kalshi general fee; order rounded up then divided per contract",
            "entry": "fresh delayed ask for candidate; original 13m ask for matched control",
        },
        "source_rows": len(rows),
        "source_row_identity_sha256": evidence_hash,
        "strict_parent_rows": len(parents),
        "valid_stage_counts": dict(sorted(
            (interval, sum(interval in stage_set for stage_set in stages.values()))
            for interval in ("12M30S", "12M", "11M30S")
        )),
        "lineage_diagnostics": lineage,
        "rules": rules,
        "interpretation": (
            "All results are pre-V18 development evidence and non-independent. "
            "Any selected rule must be frozen before a silent prospective ledger."
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    report = build_report(Path(args.strategy_db))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
