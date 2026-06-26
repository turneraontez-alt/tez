"""Latency fix: the executor order is placed BEFORE the synchronous Telegram send,
strictly inside the fired + claim_alert-won block.

The reorder must change only WHEN the order POSTs, never which/whether: it still fires
at most once per contract per window (the alert lock gates execution), never on a
non-fired/research row, and a Telegram failure can't prevent the (already-placed) order.
"""

from __future__ import annotations

import os

import pytest

from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.runner import UltoimV2Runner

MARK = {"15M": 900, "12M": 720, "10M": 600, "7M": 420}


class _Tg:
    """Telegram stub that logs send order; can raise to simulate a slow/failed send."""
    def __init__(self, log, raise_on_send=False):
        self.log = log
        self.raise_on_send = raise_on_send

    def send(self, text):
        self.log.append("telegram")
        if self.raise_on_send:
            raise RuntimeError("telegram down")
        return {"ok": True, "delivered": True, "muted": False, "message_id": 1, "error": None}


def _runner(tmp_path, log, **tg):
    cfg = UltoimV2Config(
        enabled=True, model_version="ultoim-v2", db_path=str(tmp_path / "v2.sqlite3"),
        telegram_chat_id="", min_confidence=0.55, ask_lo=50.0, ask_hi=72.0,
        min_edge_cents=2.0, mark_band_seconds=90.0, deliver_top_n=3,
        no_only=True, btc_confirm_enabled=False, require_inverse_edge=False, skip_7m=False,
    )
    r = UltoimV2Runner(cfg)
    r.telegram = _Tg(log, **tg)
    # Record executor invocations (and their order vs telegram) without touching real orders.
    r._maybe_execute = lambda *a, **k: log.append("execute")  # type: ignore[assignment]
    return r


def _no(ticker, sel=0.65, ask=60.0, close=9000.0):
    return {"asset": "BTC", "ticker": ticker, "predicted_side": "NO",
            "selected_probability": sel, "entry_ask_cents": ask, "total_cost_cents": 0.0,
            "close_time": close}


def test_executes_before_telegram(tmp_path):
    log: list[str] = []
    r = _runner(tmp_path, log)
    r._decide_interval("10M", MARK["10M"], 7, [_no("T-A")], now=1000.0)
    assert log == ["execute", "telegram"]   # order placed BEFORE the Telegram send


def test_telegram_failure_does_not_block_the_order(tmp_path):
    log: list[str] = []
    r = _runner(tmp_path, log, raise_on_send=True)
    # A failing/slow Telegram must not prevent the (already-placed) order.
    try:
        r._decide_interval("10M", MARK["10M"], 7, [_no("T-A")], now=1000.0)
    except RuntimeError:
        pass  # telegram.send raises AFTER the order is placed
    assert log[0] == "execute" and "execute" in log


def test_no_execute_on_non_fired_row(tmp_path):
    log: list[str] = []
    r = _runner(tmp_path, log)
    # sel below min_confidence => gate does not fire => recorded research-only, never executed.
    r._decide_interval("10M", MARK["10M"], 7, [_no("T-A", sel=0.50)], now=1000.0)
    assert "execute" not in log


def test_executes_at_most_once_per_contract_per_window(tmp_path):
    """Same ticker+window fired at 10M then 7M: the alert lock claims once, so the executor
    is invoked at most once — the reorder did NOT loosen the one-order-per-contract guard."""
    log: list[str] = []
    r = _runner(tmp_path, log)
    r._decide_interval("10M", MARK["10M"], 7, [_no("T-A")], now=1000.0)
    r._decide_interval("7M", MARK["7M"], 7, [_no("T-A")], now=1300.0)   # same ticker + window
    assert log.count("execute") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
