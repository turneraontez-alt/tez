"""High Volatility Flip persistence and fee-aware grading."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hvf_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    record_kind TEXT NOT NULL DEFAULT 'HIGH_VOL_FLIP_ALERT',
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    seconds_remaining REAL,
    predicted_outcome TEXT NOT NULL,
    model_predicted_side TEXT,
    rule_code TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    reason_codes TEXT,
    selected_side TEXT,
    selected_bid_cents REAL,
    selected_ask_cents REAL,
    selected_mid_cents REAL,
    previous_interval TEXT,
    previous_selected_mid_cents REAL,
    selected_mid_jump_cents REAL,
    yes_bid_cents REAL,
    yes_ask_cents REAL,
    no_bid_cents REAL,
    no_ask_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    yes_bid_depth_contracts REAL,
    yes_ask_depth_contracts REAL,
    no_bid_depth_contracts REAL,
    no_ask_depth_contracts REAL,
    kalshi_depth_status TEXT,
    kalshi_depth_missing_reason TEXT,
    kalshi_depth_retry_used INTEGER DEFAULT 0,
    kalshi_taker_yes_volume_15s REAL,
    kalshi_taker_no_volume_15s REAL,
    kalshi_taker_net_yes_volume_15s REAL,
    spot_depth_status TEXT,
    spot_depth_missing_reason TEXT,
    spot_depth_source TEXT,
    spot_depth_age_seconds REAL,
    spot_depth_trade_age_seconds REAL,
    spot_depth_best_bid REAL,
    spot_depth_best_ask REAL,
    spot_depth_mid REAL,
    spot_depth_spread_bps REAL,
    spot_depth_bid_depth_top REAL,
    spot_depth_ask_depth_top REAL,
    spot_depth_bid_depth_levels REAL,
    spot_depth_ask_depth_levels REAL,
    spot_depth_bid_notional_levels REAL,
    spot_depth_ask_notional_levels REAL,
    spot_depth_imbalance REAL,
    spot_depth_trade_buy_qty_5s REAL,
    spot_depth_trade_sell_qty_5s REAL,
    spot_depth_trade_net_qty_5s REAL,
    spot_depth_trade_buy_notional_5s REAL,
    spot_depth_trade_sell_notional_5s REAL,
    spot_depth_trade_net_notional_5s REAL,
    spot_depth_trade_buy_qty_15s REAL,
    spot_depth_trade_sell_qty_15s REAL,
    spot_depth_trade_net_qty_15s REAL,
    spot_depth_trade_buy_notional_15s REAL,
    spot_depth_trade_sell_notional_15s REAL,
    spot_depth_trade_net_notional_15s REAL,
    spot_depth_trade_buy_qty_60s REAL,
    spot_depth_trade_sell_qty_60s REAL,
    spot_depth_trade_net_qty_60s REAL,
    spot_depth_trade_buy_notional_60s REAL,
    spot_depth_trade_sell_notional_60s REAL,
    spot_depth_trade_net_notional_60s REAL,
    spot_depth_last_trade_price REAL,
    spot_depth_last_trade_side TEXT,
    spot_depth_last_trade_size REAL,
    model_yes_probability REAL,
    raw_yes_probability REAL,
    calibrated_yes_probability REAL,
    conservative_probability REAL,
    data_quality REAL,
    btc_ticker TEXT,
    btc_dominant_side TEXT,
    btc_dominant_mid_cents REAL,
    btc_selected_bid_cents REAL,
    btc_selected_ask_cents REAL,
    btc_yes_mid_cents REAL,
    btc_no_mid_cents REAL,
    btc_jump_cents REAL,
    entry_ask_cents REAL,
    entry_fee_cents REAL,
    paper_only INTEGER NOT NULL DEFAULT 1,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    message_id INTEGER,
    delivery_error TEXT,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    hypothetical_pnl_cents REAL,
    UNIQUE(model_version, ticker, window_key)
);
CREATE INDEX IF NOT EXISTS idx_hvf_resolve
    ON hvf_alerts(model_version, official_result, close_time);
CREATE INDEX IF NOT EXISTS idx_hvf_asset_rule
    ON hvf_alerts(model_version, asset, rule_code);
"""

_ADDED_COLUMNS = {
    "selected_mid_cents": "REAL",
    "previous_interval": "TEXT",
    "previous_selected_mid_cents": "REAL",
    "selected_mid_jump_cents": "REAL",
    "yes_bid_depth_contracts": "REAL",
    "yes_ask_depth_contracts": "REAL",
    "no_bid_depth_contracts": "REAL",
    "no_ask_depth_contracts": "REAL",
    "kalshi_depth_status": "TEXT",
    "kalshi_depth_missing_reason": "TEXT",
    "kalshi_depth_retry_used": "INTEGER DEFAULT 0",
    "kalshi_taker_yes_volume_15s": "REAL",
    "kalshi_taker_no_volume_15s": "REAL",
    "kalshi_taker_net_yes_volume_15s": "REAL",
    "spot_depth_status": "TEXT",
    "spot_depth_missing_reason": "TEXT",
    "spot_depth_source": "TEXT",
    "spot_depth_age_seconds": "REAL",
    "spot_depth_trade_age_seconds": "REAL",
    "spot_depth_best_bid": "REAL",
    "spot_depth_best_ask": "REAL",
    "spot_depth_mid": "REAL",
    "spot_depth_spread_bps": "REAL",
    "spot_depth_bid_depth_top": "REAL",
    "spot_depth_ask_depth_top": "REAL",
    "spot_depth_bid_depth_levels": "REAL",
    "spot_depth_ask_depth_levels": "REAL",
    "spot_depth_bid_notional_levels": "REAL",
    "spot_depth_ask_notional_levels": "REAL",
    "spot_depth_imbalance": "REAL",
    "spot_depth_trade_buy_qty_5s": "REAL",
    "spot_depth_trade_sell_qty_5s": "REAL",
    "spot_depth_trade_net_qty_5s": "REAL",
    "spot_depth_trade_buy_notional_5s": "REAL",
    "spot_depth_trade_sell_notional_5s": "REAL",
    "spot_depth_trade_net_notional_5s": "REAL",
    "spot_depth_trade_buy_qty_15s": "REAL",
    "spot_depth_trade_sell_qty_15s": "REAL",
    "spot_depth_trade_net_qty_15s": "REAL",
    "spot_depth_trade_buy_notional_15s": "REAL",
    "spot_depth_trade_sell_notional_15s": "REAL",
    "spot_depth_trade_net_notional_15s": "REAL",
    "spot_depth_trade_buy_qty_60s": "REAL",
    "spot_depth_trade_sell_qty_60s": "REAL",
    "spot_depth_trade_net_qty_60s": "REAL",
    "spot_depth_trade_buy_notional_60s": "REAL",
    "spot_depth_trade_sell_notional_60s": "REAL",
    "spot_depth_trade_net_notional_60s": "REAL",
    "spot_depth_last_trade_price": "REAL",
    "spot_depth_last_trade_side": "TEXT",
    "spot_depth_last_trade_size": "REAL",
}


def kalshi_fee_cents(entry_ask_cents: float | int | None) -> int | None:
    if entry_ask_cents is None:
        return None
    ask = max(0.0, min(100.0, float(entry_ask_cents)))
    if ask <= 0.0 or ask >= 100.0:
        return 0
    p = ask / 100.0
    return int(math.ceil(0.07 * p * (1.0 - p) * 100.0))


def net_pnl_cents(entry_ask_cents: float | int | None, correct: bool) -> float | None:
    if entry_ask_cents is None:
        return None
    ask = float(entry_ask_cents)
    fee = kalshi_fee_cents(ask)
    if fee is None:
        return None
    gross = 100.0 - ask if correct else -ask
    return gross - float(fee)


def _wilson(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    if n <= 0:
        return None, None, None
    p = right / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, centre - half, centre + half


class HighVolFlipLedger:
    _COLS = (
        "created_at", "model_version", "record_kind", "asset", "ticker",
        "interval", "window_key", "close_time", "seconds_remaining",
        "predicted_outcome", "model_predicted_side", "rule_code", "rule_name",
        "reason_codes", "selected_side", "selected_bid_cents",
        "selected_ask_cents", "selected_mid_cents", "previous_interval",
        "previous_selected_mid_cents", "selected_mid_jump_cents",
        "yes_bid_cents", "yes_ask_cents", "no_bid_cents", "no_ask_cents",
        "spread_cents", "depth_contracts", "yes_bid_depth_contracts",
        "yes_ask_depth_contracts", "no_bid_depth_contracts",
        "no_ask_depth_contracts", "kalshi_depth_status",
        "kalshi_depth_missing_reason", "kalshi_depth_retry_used",
        "kalshi_taker_yes_volume_15s", "kalshi_taker_no_volume_15s",
        "kalshi_taker_net_yes_volume_15s",
        "spot_depth_status", "spot_depth_missing_reason", "spot_depth_source",
        "spot_depth_age_seconds", "spot_depth_trade_age_seconds",
        "spot_depth_best_bid", "spot_depth_best_ask", "spot_depth_mid",
        "spot_depth_spread_bps", "spot_depth_bid_depth_top",
        "spot_depth_ask_depth_top", "spot_depth_bid_depth_levels",
        "spot_depth_ask_depth_levels", "spot_depth_bid_notional_levels",
        "spot_depth_ask_notional_levels", "spot_depth_imbalance",
        "spot_depth_trade_buy_qty_5s", "spot_depth_trade_sell_qty_5s",
        "spot_depth_trade_net_qty_5s", "spot_depth_trade_buy_notional_5s",
        "spot_depth_trade_sell_notional_5s",
        "spot_depth_trade_net_notional_5s", "spot_depth_trade_buy_qty_15s",
        "spot_depth_trade_sell_qty_15s", "spot_depth_trade_net_qty_15s",
        "spot_depth_trade_buy_notional_15s",
        "spot_depth_trade_sell_notional_15s",
        "spot_depth_trade_net_notional_15s", "spot_depth_trade_buy_qty_60s",
        "spot_depth_trade_sell_qty_60s", "spot_depth_trade_net_qty_60s",
        "spot_depth_trade_buy_notional_60s",
        "spot_depth_trade_sell_notional_60s",
        "spot_depth_trade_net_notional_60s", "spot_depth_last_trade_price",
        "spot_depth_last_trade_side", "spot_depth_last_trade_size",
        "model_yes_probability", "raw_yes_probability",
        "calibrated_yes_probability", "conservative_probability",
        "data_quality", "btc_ticker", "btc_dominant_side",
        "btc_dominant_mid_cents", "btc_selected_bid_cents",
        "btc_selected_ask_cents", "btc_yes_mid_cents", "btc_no_mid_cents",
        "btc_jump_cents", "entry_ask_cents", "entry_fee_cents", "paper_only",
        "delivery_status",
    )

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.available = True
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._ensure_columns_locked()
            self._conn.commit()

    def _ensure_columns_locked(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(hvf_alerts)").fetchall()
        }
        for name, column_type in _ADDED_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE hvf_alerts ADD COLUMN {name} {column_type}")

    def record_alert(self, row: Mapping[str, Any]) -> int | None:
        values = []
        for col in self._COLS:
            if col == "record_kind":
                values.append(row.get(col) or "HIGH_VOL_FLIP_ALERT")
            elif col == "paper_only":
                values.append(1 if row.get(col, True) else 0)
            elif col == "delivery_status":
                values.append(row.get(col) or "PENDING")
            else:
                values.append(row.get(col))
        placeholders = ",".join("?" for _ in self._COLS)
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO hvf_alerts({','.join(self._COLS)}) VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def mark_delivery(self, row_id: int, status: str, message_id: int | None,
                      error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE hvf_alerts SET delivery_status=?, message_id=?, delivery_error=? "
                "WHERE id=?",
                (status, message_id, error, row_id),
            )
            self._conn.commit()

    def alert_count(self, model_version: str, window_key: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM hvf_alerts WHERE model_version=? AND window_key=?",
                (model_version, window_key),
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def unresolved_closed(self, model_version: str, now: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM hvf_alerts "
                "WHERE model_version=? AND official_result IS NULL "
                "AND close_time IS NOT NULL AND close_time <= ? LIMIT 500",
                (model_version, now),
            ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def resolve(self, model_version: str, ticker: str, official_result: str,
                now: float | None = None) -> int:
        official = str(official_result or "").upper()
        if official not in {"YES", "NO"}:
            return 0
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, predicted_outcome, entry_ask_cents FROM hvf_alerts "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["predicted_outcome"] or "").upper()
                correct = side == official
                pnl = net_pnl_cents(r["entry_ask_cents"], correct)
                self._conn.execute(
                    "UPDATE hvf_alerts SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (official, ts, 1 if correct else 0, pnl, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    def rows(self, model_version: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hvf_alerts WHERE model_version=? ORDER BY id",
                (model_version,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_alerts(self, model_version: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hvf_alerts WHERE model_version=? "
                "ORDER BY id DESC LIMIT ?",
                (model_version, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def scoreboard(self, model_version: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hvf_alerts WHERE model_version=?",
                (model_version,),
            ).fetchall()
            delivery = dict(self._conn.execute(
                "SELECT delivery_status, COUNT(*) FROM hvf_alerts "
                "WHERE model_version=? GROUP BY delivery_status",
                (model_version,),
            ).fetchall())
        all_rows = list(rows)
        by_asset = sorted({str(r["asset"] or "") for r in all_rows if r["asset"]})
        by_rule = sorted({str(r["rule_code"] or "") for r in all_rows if r["rule_code"]})
        return {
            "available": True,
            "model_version": model_version,
            "paper_only": True,
            "total_alerts": len(all_rows),
            "resolved": sum(1 for r in all_rows if r["official_result"] is not None),
            "delivery_counts": {str(k): int(v) for k, v in delivery.items()},
            "overall": self._agg(all_rows),
            "by_asset": {
                asset: self._agg([r for r in all_rows if str(r["asset"] or "") == asset])
                for asset in by_asset
            },
            "by_rule": {
                rule: self._agg([r for r in all_rows if str(r["rule_code"] or "") == rule])
                for rule in by_rule
            },
            "by_asset_rule": {
                f"{asset}:{rule}": self._agg([
                    r for r in all_rows
                    if str(r["asset"] or "") == asset and str(r["rule_code"] or "") == rule
                ])
                for asset in by_asset for rule in by_rule
            },
        }

    @staticmethod
    def _agg(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        n_alerts = len(rows)
        resolved = [r for r in rows if r["official_result"] is not None]
        n = len(resolved)
        wins = sum(1 for r in resolved if r["correct"] == 1)
        p, lo, hi = _wilson(wins, n)
        pnls = [float(r["hypothetical_pnl_cents"]) for r in resolved
                if r["hypothetical_pnl_cents"] is not None]
        entries = [float(r["entry_ask_cents"]) for r in rows
                   if r["entry_ask_cents"] is not None]
        return {
            "alerts": n_alerts,
            "resolved": n,
            "wins": wins,
            "losses": n - wins,
            "accuracy": p,
            "ci_low": lo,
            "ci_high": hi,
            "net_pnl_cents": round(sum(pnls), 2) if pnls else 0.0,
            "avg_pnl_cents": round(sum(pnls) / len(pnls), 3) if pnls else None,
            "avg_entry_price_cents": round(sum(entries) / len(entries), 3) if entries else None,
        }
