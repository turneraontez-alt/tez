"""Read-only executor P&L and exit-counterfactual reporting helpers."""
from __future__ import annotations

from collections import defaultdict, deque
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

VALID_RESULTS = {"YES", "NO"}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _json_obj(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        inner = value.get("order")
        return inner if isinstance(inner, Mapping) else value
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(data, Mapping):
        inner = data.get("order")
        return inner if isinstance(inner, Mapping) else data
    return {}


def _cents(value: Any) -> float | None:
    num = _num(value)
    if num is None:
        return None
    return num * 100.0 if 0.0 <= num <= 1.0 else num


def _first_cents(payload: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        out = _cents(payload.get(name))
        if out is not None:
            return out
    return None


def parse_order_prices(row: Mapping[str, Any], *, prefer_final: bool = False) -> dict[str, Any]:
    """Extract YES-denominated average fill price and per-contract fee in cents.

    Missing fields stay ``None``. Callers can decide whether to compute gross-only
    metrics or skip rows that lack fee data.
    """
    payload = _json_obj(
        row.get("final_response_json") if prefer_final and row.get("final_response_json")
        else row.get("response_json")
    )
    yes_price = None
    fee = None
    if prefer_final:
        yes_price = _cents(row.get("final_average_fill_price"))
        fee = _cents(row.get("final_average_fee_paid"))
    if yes_price is None:
        yes_price = _first_cents(
            payload,
            "average_fill_price",
            "avg_fill_price",
            "average_price",
            "avg_price",
            "price",
        )
    if fee is None:
        fee = _first_cents(
            payload,
            "average_fee_paid",
            "avg_fee_paid",
            "fee_paid",
            "fees",
        )
    return {
        "yes_price_cents": yes_price,
        "fee_cents": fee,
        "price_source": "recorded_order_payload" if yes_price is not None else None,
        "fee_source": "recorded_order_payload" if fee is not None else None,
    }


def _position_side(row: Mapping[str, Any]) -> str | None:
    explicit = str(row.get("position_side") or row.get("contract_side") or row.get("side") or "").upper()
    if explicit in VALID_RESULTS:
        return explicit
    coid = str(row.get("client_order_id") or "")
    if coid.startswith("v2xy-"):
        return "YES"
    if coid.startswith("v2x-"):
        return "NO"
    action = str(row.get("action") or "").lower()
    payload_side = str(_json_obj(row.get("response_json")).get("side") or "").lower()
    if action == "entry" and payload_side == "bid":
        return "YES"
    if action == "entry" and payload_side == "ask":
        return "NO"
    if action == "exit" and payload_side == "ask":
        return "YES"
    if action == "exit" and payload_side == "bid":
        return "NO"
    return None


def _filled_count(row: Mapping[str, Any], *, prefer_final: bool = False) -> tuple[int | None, str | None]:
    if prefer_final and row.get("final_fill_status") is not None:
        status = str(row.get("final_fill_status") or "").upper()
        count = row.get("final_filled_count")
    else:
        status = str(row.get("fill_status") or "").upper()
        count = row.get("filled_count")
    if status not in {"FILLED", "PARTIAL"}:
        return 0, status or None
    num = _num(count)
    return (int(num), status) if num is not None else (None, status)


def _side_price_cents(side: str, yes_price_cents: float) -> float:
    return yes_price_cents if side == "YES" else 100.0 - yes_price_cents


def reconstruct_order_pnl(
    order_rows: Iterable[Mapping[str, Any]],
    result_lookup: Mapping[str, str],
    *,
    prefer_final: bool = False,
) -> dict[str, Any]:
    """Reconstruct FIFO realized P&L and exit-vs-hold counterfactuals from provided rows.

    ``result_lookup`` must map ticker to official ``YES``/``NO``. Rows without
    fill count, average fill price, fee, side, or official result are reported
    explicitly and excluded from the metric that requires the missing field.
    """
    rows = sorted((dict(r) for r in order_rows), key=lambda r: float(r.get("created_at") or 0.0))
    lots: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []
    settlement_rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    summary = {
        "orders": len(rows),
        "filled_orders": 0,
        "entry_contracts": 0,
        "exit_contracts": 0,
        "settlement_contracts": 0,
        "exit_pnl_cents": 0.0,
        "settlement_pnl_cents": 0.0,
        "total_pnl_cents": 0.0,
        "exit_minus_hold_cents": 0.0,
        "missing_result_contracts": 0,
        "unmatched_exit_contracts": 0,
        "missing_price_orders": 0,
        "missing_fee_orders": 0,
        "missing_side_orders": 0,
        "missing_fill_count_orders": 0,
    }

    for row in rows:
        count, status = _filled_count(row, prefer_final=prefer_final)
        if count == 0:
            continue
        if count is None:
            summary["missing_fill_count_orders"] += 1
            issues.append({"ticker": row.get("ticker"), "issue": "missing_fill_count", "status": status})
            continue
        summary["filled_orders"] += 1
        ticker = str(row.get("ticker") or "")
        action = str(row.get("action") or "").lower()
        side = _position_side(row)
        if side is None:
            summary["missing_side_orders"] += 1
            issues.append({"ticker": ticker, "issue": "missing_side"})
            continue
        parsed = parse_order_prices(row, prefer_final=prefer_final)
        yes_price = parsed["yes_price_cents"]
        fee = parsed["fee_cents"]
        if yes_price is None:
            summary["missing_price_orders"] += 1
            issues.append({"ticker": ticker, "issue": "missing_average_fill_price"})
            continue
        if fee is None:
            summary["missing_fee_orders"] += 1
            issues.append({"ticker": ticker, "issue": "missing_average_fee_paid"})
            continue
        side_price = _side_price_cents(side, float(yes_price))
        key = (ticker, side)
        if action == "entry":
            summary["entry_contracts"] += count
            lots[key].append({
                "count": count,
                "entry_price_cents": side_price,
                "entry_fee_cents": float(fee),
                "ticker": ticker,
                "side": side,
                "row_id": row.get("id"),
            })
            continue
        if action != "exit":
            issues.append({"ticker": ticker, "issue": f"unsupported_action:{action}"})
            continue
        remaining = count
        while remaining > 0 and lots[key]:
            lot = lots[key][0]
            matched = min(remaining, int(lot["count"]))
            exit_pnl = (side_price - lot["entry_price_cents"] - lot["entry_fee_cents"] - float(fee)) * matched
            official = str(result_lookup.get(ticker) or "").upper()
            hold_pnl = None
            exit_minus_hold = None
            if official in VALID_RESULTS:
                payout = 100.0 if official == side else 0.0
                hold_pnl = (payout - lot["entry_price_cents"] - lot["entry_fee_cents"]) * matched
                exit_minus_hold = exit_pnl - hold_pnl
                summary["exit_minus_hold_cents"] += exit_minus_hold
            else:
                summary["missing_result_contracts"] += matched
            record = {
                "ticker": ticker,
                "side": side,
                "contracts": matched,
                "entry_price_cents": lot["entry_price_cents"],
                "exit_price_cents": side_price,
                "official_result": official or None,
                "exit_pnl_cents": exit_pnl,
                "hold_pnl_cents": hold_pnl,
                "exit_minus_hold_cents": exit_minus_hold,
            }
            closed.append(record)
            summary["exit_contracts"] += matched
            summary["exit_pnl_cents"] += exit_pnl
            summary["total_pnl_cents"] += exit_pnl
            remaining -= matched
            lot["count"] -= matched
            if lot["count"] <= 0:
                lots[key].popleft()
        if remaining > 0:
            summary["unmatched_exit_contracts"] += remaining
            issues.append({"ticker": ticker, "issue": "exit_without_open_lot", "contracts": remaining})

    for (ticker, side), queue in lots.items():
        official = str(result_lookup.get(ticker) or "").upper()
        for lot in queue:
            count = int(lot["count"])
            if official not in VALID_RESULTS:
                summary["missing_result_contracts"] += count
                issues.append({"ticker": ticker, "issue": "missing_official_result", "contracts": count})
                continue
            payout = 100.0 if official == side else 0.0
            pnl = (payout - lot["entry_price_cents"] - lot["entry_fee_cents"]) * count
            record = {
                "ticker": ticker,
                "side": side,
                "contracts": count,
                "entry_price_cents": lot["entry_price_cents"],
                "official_result": official,
                "settlement_pnl_cents": pnl,
            }
            settlement_rows.append(record)
            summary["settlement_contracts"] += count
            summary["settlement_pnl_cents"] += pnl
            summary["total_pnl_cents"] += pnl

    for key in ("exit_pnl_cents", "settlement_pnl_cents", "total_pnl_cents", "exit_minus_hold_cents"):
        summary[key] = round(float(summary[key]), 4)
    return {"summary": summary, "closed_trades": closed, "settlement_rows": settlement_rows, "issues": issues}


def load_executor_orders(path: str | Path) -> list[dict[str, Any]]:
    db_path = Path(path)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executor_orders'"
        ).fetchone()
        if not exists:
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM executor_orders ORDER BY created_at, id")]
    finally:
        conn.close()


def load_official_results(paths: Iterable[str | Path]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Read ticker -> official_result from any sqlite table that has both columns."""
    results: dict[str, str] = {}
    conflicts: dict[str, set[str]] = {}
    for path in paths:
        db_path = Path(path)
        if not db_path.exists():
            continue
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            tables = [
                str(r["name"])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                quoted = _quote_ident(table)
                cols = {
                    str(r["name"])
                    for r in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                }
                if "ticker" not in cols or "official_result" not in cols:
                    continue
                for row in conn.execute(
                    f"SELECT ticker, official_result FROM {quoted} "
                    "WHERE official_result IN ('YES','NO') AND ticker IS NOT NULL"
                ):
                    ticker = str(row["ticker"])
                    result = str(row["official_result"]).upper()
                    if ticker in conflicts:
                        conflicts[ticker].add(result)
                        continue
                    prev = results.get(ticker)
                    if prev is not None and prev != result:
                        conflicts[ticker] = {prev, result}
                        results.pop(ticker, None)
                    else:
                        results[ticker] = result
        finally:
            conn.close()
    return results, conflicts
