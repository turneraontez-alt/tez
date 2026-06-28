"""SQLite persistence for asset-specific strategy-bot decisions."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .rules import (
    ACCEPTED,
    KALSHI_DEPTH_KEYS,
    KALSHI_FLOW_KEYS,
    RESEARCH_ONLY,
    SPOT_DEPTH_KEYS,
    STRATEGY_VERSION,
    BotDecision,
    source_rule,
    source_side,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_bot_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    bot_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    decision_mode TEXT NOT NULL DEFAULT 'PAPER_RESEARCH',
    paper_only INTEGER NOT NULL DEFAULT 1,
    reason_codes TEXT,
    reason_json TEXT,
    threshold_json TEXT,
    source_system TEXT NOT NULL,
    source_model_version TEXT,
    source_rule TEXT,
    source_rule_name TEXT,
    source_reason_codes TEXT,
    record_kind TEXT,
    delivery_status TEXT,
    asset TEXT,
    side TEXT,
    interval TEXT,
    window_key INTEGER,
    ticker TEXT,
    close_time REAL,
    entry_ask_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    yes_bid_depth_contracts REAL,
    yes_ask_depth_contracts REAL,
    no_bid_depth_contracts REAL,
    no_ask_depth_contracts REAL,
    kalshi_depth_status TEXT,
    kalshi_depth_missing_reason TEXT,
    kalshi_depth_retry_used INTEGER,
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
    btc_context_json TEXT,
    btc_ticker TEXT,
    btc_depth_contracts REAL,
    btc_book_pressure_cents REAL,
    btc_dominant_side TEXT,
    btc_model_predicted_side TEXT,
    btc_model_yes_probability REAL,
    btc_calibrated_yes_probability REAL,
    btc_market_implied_yes_probability REAL,
    notification_status TEXT,
    notification_message_id INTEGER,
    notification_error TEXT,
    notified_at REAL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    hypothetical_pnl_cents REAL,
    UNIQUE(
        strategy_version, bot_name, source_system, source_model_version,
        ticker, interval, window_key, source_rule
    )
);
CREATE INDEX IF NOT EXISTS idx_strategy_bot_resolve
    ON strategy_bot_decisions(source_system, source_model_version, ticker, official_result);
CREATE INDEX IF NOT EXISTS idx_strategy_bot_score
    ON strategy_bot_decisions(strategy_version, bot_name, decision_status, asset, side);
"""

_COLS = (
    "created_at",
    "bot_name",
    "strategy_version",
    "decision_status",
    "decision_mode",
    "paper_only",
    "reason_codes",
    "reason_json",
    "threshold_json",
    "source_system",
    "source_model_version",
    "source_rule",
    "source_rule_name",
    "source_reason_codes",
    "record_kind",
    "delivery_status",
    "asset",
    "side",
    "interval",
    "window_key",
    "ticker",
    "close_time",
    "entry_ask_cents",
    "spread_cents",
    *KALSHI_DEPTH_KEYS,
    *KALSHI_FLOW_KEYS,
    *SPOT_DEPTH_KEYS,
    "btc_context_json",
    "btc_ticker",
    "btc_depth_contracts",
    "btc_book_pressure_cents",
    "btc_dominant_side",
    "btc_model_predicted_side",
    "btc_model_yes_probability",
    "btc_calibrated_yes_probability",
    "btc_market_implied_yes_probability",
    "notification_status",
    "notification_message_id",
    "notification_error",
    "notified_at",
    "official_result",
    "resolved_at",
    "correct",
    "hypothetical_pnl_cents",
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def kalshi_fee_cents(entry_ask_cents: float | int | None) -> int | None:
    if entry_ask_cents is None:
        return None
    ask = max(0.0, min(100.0, float(entry_ask_cents)))
    if ask <= 0.0 or ask >= 100.0:
        return 0
    p = ask / 100.0
    return int(math.ceil(0.07 * p * (1.0 - p) * 100.0))


def net_pnl_cents(entry_ask_cents: float | int | None, correct: bool) -> float | None:
    ask = _num(entry_ask_cents)
    if ask is None:
        return None
    fee = kalshi_fee_cents(ask)
    if fee is None:
        return None
    gross = 100.0 - ask if correct else -ask
    return gross - float(fee)


def _json(data: Any) -> str | None:
    if data is None:
        return None

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [clean(v) for v in value]
        if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        try:
            out = float(value)
            return out if math.isfinite(out) else str(value)
        except (TypeError, ValueError):
            return str(value)

    return json.dumps(clean(data), sort_keys=True, separators=(",", ":"))


def _csv(values: Sequence[str]) -> str:
    return ",".join(str(v) for v in values if str(v))


def _entry_ask(row: Mapping[str, Any]) -> Any:
    return row.get("entry_ask_cents") if row.get("entry_ask_cents") is not None else row.get("selected_ask_cents")


def _source_model(row: Mapping[str, Any]) -> str | None:
    return str(row.get("model_version")) if row.get("model_version") is not None else None


def _build_record(
    decision: BotDecision,
    row: Mapping[str, Any],
    source_system: str,
) -> dict[str, Any]:
    btc = dict(decision.btc_context or {})
    created = _num(row.get("created_at")) or time.time()
    out: dict[str, Any] = {
        "created_at": created,
        "bot_name": decision.bot_name,
        "strategy_version": decision.strategy_version,
        "decision_status": decision.decision_status,
        "decision_mode": "PAPER_RESEARCH",
        "paper_only": 1,
        "reason_codes": _csv(decision.reason_codes),
        "reason_json": _json(list(decision.reason_codes)),
        "threshold_json": _json(decision.threshold_profile),
        "source_system": source_system,
        "source_model_version": _source_model(row),
        "source_rule": source_rule(row),
        "source_rule_name": row.get("rule_name"),
        "source_reason_codes": row.get("reason_codes"),
        "record_kind": row.get("record_kind"),
        "delivery_status": row.get("delivery_status"),
        "asset": row.get("asset"),
        "side": source_side(row),
        "interval": row.get("interval"),
        "window_key": row.get("window_key"),
        "ticker": row.get("ticker"),
        "close_time": row.get("close_time"),
        "entry_ask_cents": _entry_ask(row),
        "spread_cents": row.get("spread_cents"),
        "btc_context_json": _json(btc) if btc else None,
        "btc_ticker": btc.get("btc_ticker"),
        "btc_depth_contracts": btc.get("btc_depth_contracts"),
        "btc_book_pressure_cents": btc.get("btc_book_pressure_cents"),
        "btc_dominant_side": btc.get("btc_dominant_side"),
        "btc_model_predicted_side": btc.get("btc_model_predicted_side"),
        "btc_model_yes_probability": btc.get("btc_model_yes_probability"),
        "btc_calibrated_yes_probability": btc.get("btc_calibrated_yes_probability"),
        "btc_market_implied_yes_probability": btc.get("btc_market_implied_yes_probability"),
        "notification_status": None,
        "notification_message_id": None,
        "notification_error": None,
        "notified_at": None,
        "official_result": None,
        "resolved_at": None,
        "correct": None,
        "hypothetical_pnl_cents": None,
    }
    for key in KALSHI_DEPTH_KEYS:
        out[key] = row.get(key)
    for key in KALSHI_FLOW_KEYS:
        out[key] = row.get(key)
    for key in SPOT_DEPTH_KEYS:
        out[key] = row.get(key)
    return out


class StrategyBotLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._ensure_columns_locked()
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_columns_locked(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(strategy_bot_decisions)").fetchall()
        }
        added = {
            "notification_status": "TEXT",
            "notification_message_id": "INTEGER",
            "notification_error": "TEXT",
            "notified_at": "REAL",
        }
        for name, column_type in added.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE strategy_bot_decisions ADD COLUMN {name} {column_type}"
                )

    def record_decision(
        self,
        decision: BotDecision,
        source_row: Mapping[str, Any],
        *,
        source_system: str,
    ) -> int | None:
        row = _build_record(decision, source_row, source_system)
        placeholders = ",".join("?" for _ in _COLS)
        values = [row.get(c) for c in _COLS]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO strategy_bot_decisions({','.join(_COLS)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def mark_notification(
        self,
        row_id: int,
        *,
        status: str,
        message_id: int | None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE strategy_bot_decisions SET notification_status=?, "
                "notification_message_id=?, notification_error=?, notified_at=? WHERE id=?",
                (status, message_id, error, ts, row_id),
            )
            self._conn.commit()

    def row_by_id(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_bot_decisions WHERE id=?",
                (int(row_id),),
            ).fetchone()
        return None if row is None else dict(row)

    def has_accepted_window(
        self,
        *,
        bot_name: str,
        strategy_version: str,
        asset: str,
        side: str,
        window_key: int,
        ticker: str | None = None,
    ) -> bool:
        query = (
            "SELECT 1 FROM strategy_bot_decisions "
            "WHERE bot_name=? AND strategy_version=? AND asset=? AND side=? "
            "AND window_key=? AND decision_status=?"
        )
        params: list[Any] = [bot_name, strategy_version, asset, side, window_key, ACCEPTED]
        if ticker:
            query += " AND ticker<>?"
            params.append(ticker)
        query += " LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return row is not None

    def resolve(
        self,
        *,
        source_system: str,
        source_model_version: str,
        ticker: str,
        official_result: str,
        now: float | None = None,
    ) -> int:
        official = str(official_result or "").upper()
        if official not in {"YES", "NO"}:
            return 0
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, side, entry_ask_cents FROM strategy_bot_decisions "
                "WHERE source_system=? AND source_model_version=? AND ticker=? "
                "AND official_result IS NULL",
                (source_system, source_model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["side"] or "").upper()
                correct = side == official
                pnl = net_pnl_cents(r["entry_ask_cents"], correct)
                self._conn.execute(
                    "UPDATE strategy_bot_decisions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (official, ts, 1 if correct else 0, pnl, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    def rows(self, strategy_version: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM strategy_bot_decisions"
        params: tuple[Any, ...] = ()
        if strategy_version:
            query += " WHERE strategy_version=?"
            params = (strategy_version,)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def scoreboard(
        self,
        strategy_version: str = STRATEGY_VERSION,
        *,
        min_n: int = 30,
    ) -> dict[str, Any]:
        rows = self.rows(strategy_version)
        return {
            "available": True,
            "strategy_version": strategy_version,
            "paper_only": True,
            "min_n": int(min_n),
            "total_rows": len(rows),
            "resolved": sum(1 for r in rows if r.get("official_result") is not None),
            "accepted": self._agg([r for r in rows if r.get("decision_status") == ACCEPTED], min_n),
            "research_only": self._agg(
                [r for r in rows if r.get("decision_status") == RESEARCH_ONLY], min_n
            ),
            "all": self._agg(rows, min_n),
            "by_bot": self._group(rows, ("bot_name",), min_n),
            "by_bot_status": self._group(rows, ("bot_name", "decision_status"), min_n),
            "by_bot_asset": self._group(rows, ("bot_name", "asset"), min_n),
            "by_bot_asset_side": self._group(rows, ("bot_name", "asset", "side"), min_n),
            "by_bot_rule": self._group(rows, ("bot_name", "source_rule"), min_n),
            "by_bot_interval": self._group(rows, ("bot_name", "interval"), min_n),
            "by_bot_delivery_status": self._group(rows, ("bot_name", "delivery_status"), min_n),
            "by_bot_rule_interval_delivery": self._group(
                rows, ("bot_name", "source_rule", "interval", "delivery_status"), min_n
            ),
            "accepted_by_bot_asset_side_rule_interval_delivery": self._group(
                [r for r in rows if r.get("decision_status") == ACCEPTED],
                ("bot_name", "asset", "side", "source_rule", "interval", "delivery_status"),
                min_n,
            ),
        }

    @classmethod
    def _group(
        cls,
        rows: Sequence[Mapping[str, Any]],
        keys: Sequence[str],
        min_n: int,
    ) -> dict[str, Any]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            label = "|".join(str(row.get(k) if row.get(k) is not None else "") for k in keys)
            groups.setdefault(label, []).append(row)
        return {label: cls._agg(group, min_n) for label, group in sorted(groups.items())}

    @staticmethod
    def _agg(rows: Sequence[Mapping[str, Any]], min_n: int) -> dict[str, Any]:
        all_rows = list(rows)
        settled = [r for r in all_rows if r.get("official_result") is not None]
        right = sum(1 for r in settled if int(r.get("correct") or 0) == 1)
        pnls = [
            float(r["hypothetical_pnl_cents"])
            for r in settled
            if r.get("hypothetical_pnl_cents") is not None
        ]
        n = len(settled)
        return {
            "rows": len(all_rows),
            "resolved": n,
            "correct": right,
            "accuracy": None if n <= 0 else right / n,
            "avg_pnl_cents": None if not pnls else sum(pnls) / len(pnls),
            "net_pnl_cents": None if not pnls else sum(pnls),
            "provisional": n < int(min_n),
        }
