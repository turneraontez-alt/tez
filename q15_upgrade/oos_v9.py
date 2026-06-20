"""Chronological out-of-sample evaluation for stored Q15 checkpoint decisions.

This is a forward-record replay, not a fabricated tick-level backtest. Metrics
are returned only when enough uniquely resolved checkpoint observations exist.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

VERSION = "q15-v9-oos-evaluator"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _prob(value: Any) -> float | None:
    number = _num(value)
    if number is None:
        return None
    if 1 < number <= 100:
        number /= 100.0
    if not 0 <= number <= 1:
        return None
    return min(0.999999, max(0.000001, number))


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, Mapping) else {}
        except Exception:
            return {}
    return {}


def _actual_yes(row: Mapping[str, Any]) -> int | None:
    side = str(row.get("actual_side") or "").upper()
    if side == "YES":
        return 1
    if side == "NO":
        return 0
    correct = row.get("correct")
    predicted = str(row.get("predicted_side") or "").upper()
    if correct is not None and predicted in {"YES", "NO"}:
        is_correct = bool(correct)
        result = predicted if is_correct else ("NO" if predicted == "YES" else "YES")
        return 1 if result == "YES" else 0
    return None


def _market_yes(row: Mapping[str, Any]) -> float | None:
    side_probability = _prob(row.get("market_implied_side_probability"))
    side = str(row.get("predicted_side") or "").upper()
    if side_probability is None or side not in {"YES", "NO"}:
        return None
    return side_probability if side == "YES" else 1.0 - side_probability


def _yes_probability(row: Mapping[str, Any]) -> float | None:
    direct = _prob(row.get("yes_probability"))
    if direct is not None:
        return direct
    side_probability = _prob(row.get("side_probability"))
    side = str(row.get("predicted_side") or "").upper()
    if side_probability is None or side not in {"YES", "NO"}:
        return None
    return side_probability if side == "YES" else 1.0 - side_probability


def _costs(row: Mapping[str, Any]) -> float | None:
    features = _json(row.get("features_json"))
    economics = _json(features.get("canonical_economics"))
    value = _num(economics.get("total_estimated_cost_cents"))
    if value is not None:
        return max(0.0, value)
    value = _num(features.get("total_estimated_cost_cents"))
    return max(0.0, value) if value is not None else None


def _pnl(row: Mapping[str, Any]) -> float | None:
    ask = _num(row.get("executable_ask_cents"))
    actual = _actual_yes(row)
    predicted = str(row.get("predicted_side") or "").upper()
    costs = _costs(row)
    if ask is None or actual is None or predicted not in {"YES", "NO"} or costs is None:
        return None
    won = (actual == 1 and predicted == "YES") or (actual == 0 and predicted == "NO")
    return (100.0 - ask - costs) if won else (-ask - costs)


def _mean(values: Iterable[float]) -> float | None:
    rows = list(values)
    return statistics.fmean(rows) if rows else None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = []
    market_pairs = []
    pnl_values = []
    for row in rows:
        actual = _actual_yes(row)
        probability = _yes_probability(row)
        if actual is None or probability is None:
            continue
        evaluated.append((probability, actual, row))
        market_probability = _market_yes(row)
        if market_probability is not None:
            market_pairs.append((market_probability, actual))
        pnl = _pnl(row)
        if pnl is not None:
            pnl_values.append(pnl)

    if not evaluated:
        return {"available": False, "reason": "no_valid_probability_outcomes"}

    brier = _mean((p - y) ** 2 for p, y, _ in evaluated)
    log_loss = _mean(-(y * math.log(p) + (1 - y) * math.log(1 - p)) for p, y, _ in evaluated)
    correct = sum((p >= 0.5) == bool(y) for p, y, _ in evaluated)
    market_brier = _mean((p - y) ** 2 for p, y in market_pairs)

    buckets: dict[str, list[tuple[float, int]]] = {}
    for probability, actual, _ in evaluated:
        lower = int(probability * 10) * 10
        lower = min(lower, 90)
        label = f"{lower}-{lower + 9}%" if lower < 90 else "90-100%"
        buckets.setdefault(label, []).append((probability, actual))
    calibration = []
    weighted_error = 0.0
    for label, values in sorted(buckets.items()):
        predicted = _mean(p for p, _ in values) or 0.0
        observed = _mean(float(y) for _, y in values) or 0.0
        weighted_error += len(values) * abs(predicted - observed)
        calibration.append({
            "bucket": label,
            "n": len(values),
            "mean_predicted_yes": round(predicted, 6),
            "actual_yes_rate": round(observed, 6),
        })
    calibration_error = weighted_error / len(evaluated)

    by_asset: dict[str, dict[str, int]] = {}
    for probability, actual, row in evaluated:
        asset = str(row.get("asset") or "UNKNOWN")
        record = by_asset.setdefault(asset, {"n": 0, "correct": 0})
        record["n"] += 1
        record["correct"] += int((probability >= 0.5) == bool(actual))
    by_asset_output = {
        asset: {**record, "accuracy": round(record["correct"] / record["n"], 6)}
        for asset, record in by_asset.items()
    }

    return {
        "available": True,
        "n": len(evaluated),
        "accuracy": round(correct / len(evaluated), 6),
        "brier_score": None if brier is None else round(brier, 8),
        "log_loss": None if log_loss is None else round(log_loss, 8),
        "calibration_error": round(calibration_error, 8),
        "market_baseline_n": len(market_pairs),
        "market_baseline_brier_score": None if market_brier is None else round(market_brier, 8),
        "brier_improvement_vs_market": None if brier is None or market_brier is None else round(market_brier - brier, 8),
        "calibration": calibration,
        "by_asset": by_asset_output,
        "pnl_available": bool(pnl_values),
        "pnl_n": len(pnl_values),
        "net_simulated_pnl_cents": round(sum(pnl_values), 6) if pnl_values else None,
        "average_net_pnl_cents": round(statistics.fmean(pnl_values), 6) if pnl_values else None,
        "pnl_note": None if pnl_values else "Stored rows do not contain complete canonical execution costs; P&L was not fabricated.",
    }


class OutOfSampleEvaluator:
    def __init__(self, store: Any = None, sqlite_path: str | None = None):
        self.store = store
        self.sqlite_path = Path(sqlite_path or os.getenv("Q15_V8_SQLITE_PATH", "data/q15_10m_reviews.sqlite3"))
        self.minimum_resolved = max(10, int(os.getenv("Q15_V9_OOS_MIN_RESOLVED", "30")))
        self.test_fraction = min(0.50, max(0.20, float(os.getenv("Q15_V9_OOS_TEST_FRACTION", "0.30"))))

    def _postgres_rows(self) -> list[dict[str, Any]]:
        if not bool(getattr(self.store, "enabled", False)) or not hasattr(self.store, "query"):
            return []
        # These review tables may never have been created. Querying a missing
        # relation makes SignalStore.query log "relation does not exist" and roll
        # back the txn every cycle. to_regclass() returns NULL (no error) for an
        # absent relation, so we probe existence first and only SELECT what's there.
        candidate_tables = ("q15_ten_minute_reviews", "q15_10m_reviews")
        for table in candidate_tables:
            probe = self.store.query("SELECT to_regclass(%s) AS reg", (table,))
            if not probe or dict(probe[0]).get("reg") is None:
                continue
            rows = self.store.query(
                f"SELECT * FROM {table} WHERE status='RESOLVED' "
                "AND actual_side IN ('YES','NO') ORDER BY predicted_at ASC"
            )
            if rows:
                return [dict(row) for row in rows]
        return []

    def _sqlite_rows(self) -> list[dict[str, Any]]:
        if not self.sqlite_path.is_file():
            return []
        try:
            connection = sqlite3.connect(str(self.sqlite_path))
            connection.row_factory = sqlite3.Row
            table_names = {
                row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            table = "ten_minute_predictions" if "ten_minute_predictions" in table_names else "q15_ten_minute_reviews" if "q15_ten_minute_reviews" in table_names else None
            if not table:
                connection.close()
                return []
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE status='RESOLVED' AND actual_side IN ('YES','NO') ORDER BY predicted_at ASC"
            ).fetchall()
            connection.close()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def resolved_rows(self) -> list[dict[str, Any]]:
        rows = self._postgres_rows() or self._sqlite_rows()
        # Stable unique decision identity; repeated poll snapshots are excluded.
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("prediction_key") or f"{row.get('ticker')}:{row.get('predicted_at')}:{row.get('model_version')}")
            unique[key] = row
        return sorted(unique.values(), key=lambda row: str(row.get("predicted_at") or ""))

    def out_of_sample_report(self) -> dict[str, Any]:
        rows = self.resolved_rows()
        base = {
            "version": VERSION,
            "generated_at": time.time(),
            "evaluation_type": "chronological stored-checkpoint forward replay",
            "unique_resolved_observations": len(rows),
            "minimum_required": self.minimum_resolved,
            "random_split_used": False,
            "future_data_allowed": False,
        }
        if len(rows) < self.minimum_resolved:
            return {
                **base,
                "available": False,
                "reason": "insufficient_historical_resolved_observations",
                "resolved_outcome_metrics_available": False,
            }
        split_index = max(1, int(len(rows) * (1.0 - self.test_fraction)))
        training = rows[:split_index]
        test = rows[split_index:]
        if len(test) < max(5, self.minimum_resolved // 5):
            return {
                **base,
                "available": False,
                "reason": "insufficient_untouched_test_observations",
                "resolved_outcome_metrics_available": False,
            }
        metrics = _metrics(test)
        return {
            **base,
            "available": bool(metrics.get("available")),
            "resolved_outcome_metrics_available": bool(metrics.get("available")),
            "train_observations": len(training),
            "untouched_test_observations": len(test),
            "split_timestamp": test[0].get("predicted_at") if test else None,
            "metrics": metrics,
            "claim_of_edge": (
                "supported_on_current untouched sample"
                if metrics.get("available")
                and (metrics.get("brier_improvement_vs_market") or 0) > 0
                and metrics.get("pnl_available")
                and (metrics.get("average_net_pnl_cents") or 0) > 0
                else "not established"
            ),
        }

    def backtest_report(self) -> dict[str, Any]:
        report = self.out_of_sample_report()
        return {
            **report,
            "backtest_type": "stored forward decisions only",
            "full_tick_replay_available": False,
            "note": "No full historical order-book replay is claimed. Only predictions frozen at their original checkpoint timestamps are evaluated.",
        }

    def model_comparison(self) -> dict[str, Any]:
        report = self.out_of_sample_report()
        if not report.get("available"):
            return report
        metrics = report.get("metrics") or {}
        return {
            "version": VERSION,
            "available": True,
            "test_observations": report.get("untouched_test_observations"),
            "model_brier": metrics.get("brier_score"),
            "market_baseline_brier": metrics.get("market_baseline_brier_score"),
            "model_minus_market_brier_improvement": metrics.get("brier_improvement_vs_market"),
            "model_net_expectancy_cents": metrics.get("average_net_pnl_cents"),
            "edge_established": report.get("claim_of_edge") != "not established",
            "warning": "Promotion still requires repeated walk-forward/shadow confirmation; one holdout is not proof of future profitability.",
        }
