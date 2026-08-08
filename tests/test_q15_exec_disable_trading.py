"""Filesystem trading-disable switch (scripts/disable_trading.sh).

When the configured disable_file exists, the executor refuses ALL new ENTRIES (NO book + YES
bot) on the next fire — a live kill with no restart — while defensive EXITS stay allowed so an
open position can still be closed. Default-inert: with no file present, behaviour is unchanged.
Pure / dry-run; no network.
"""

from __future__ import annotations

import os
import subprocess

from q15_upgrade.executor.config import ExecutorConfig
from q15_upgrade.executor.executor import Executor


class _StubClient:
    def __init__(self):
        self.orders = []

    def get_balance_cents(self):
        return None

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"ok": True, "dry_run": True, "would_place": kw}


def _cfg(disable_file, **over):
    base = dict(enabled=True, dry_run=True, bankroll_cents=1_000_000,
                stake_ladder_cents=(27500, 15000), max_picks_per_window=2,
                min_price_cents=50, max_price_cents=85, btc_gate_enabled=False,
                record_orders=False, disable_file=disable_file)
    base.update(over)
    return ExecutorConfig(**base)


def _fire(ex, ticker="T-A", price=50, wk=7, side="NO", **extra):
    p = {"asset": "BTC", "predicted_side": side, "entry_ask_cents": price,
         "window_key": wk, "interval": "10M", "ticker": ticker}
    p.update(extra)
    return ex.on_fire(p)


# --------------------------------------------------------------------------- #
# the runtime gate
# --------------------------------------------------------------------------- #
def test_entry_blocked_when_disable_file_present(tmp_path):
    kf = tmp_path / "EXECUTOR_DISABLED"
    kf.write_text("halted")
    ex = Executor(_cfg(str(kf)), client=_StubClient())
    r = _fire(ex)
    assert r["placed"] is False and r["reason"] == "TRADING_DISABLED"
    assert ex.client.orders == []                  # nothing was sent to the broker


def test_entry_allowed_when_file_absent(tmp_path):
    kf = tmp_path / "EXECUTOR_DISABLED"             # never created
    ex = Executor(_cfg(str(kf)), client=_StubClient())
    r = _fire(ex)
    assert r["placed"] is True and r["stake_cents"] == 27500
    assert len(ex.client.orders) == 1


def test_gate_is_live_remove_file_resumes(tmp_path):
    kf = tmp_path / "EXECUTOR_DISABLED"
    kf.write_text("halted")
    ex = Executor(_cfg(str(kf)), client=_StubClient())
    assert _fire(ex, ticker="T-A")["reason"] == "TRADING_DISABLED"
    os.remove(kf)                                   # resume mid-process — no restart
    assert _fire(ex, ticker="T-B")["placed"] is True


def test_yes_bot_also_halts(tmp_path):
    kf = tmp_path / "EXECUTOR_DISABLED"
    kf.write_text("halted")
    yes = Executor(_cfg(str(kf), no_only=False, min_price_cents=50, max_price_cents=99,
                        min_yes_prob=0.55, min_btc_lean=0.55), client=_StubClient())
    r = _fire(yes, side="YES", price=60, yes_prob=0.60, btc_lean=0.60)
    assert r["placed"] is False and r["reason"] == "TRADING_DISABLED"


def test_defensive_exit_still_allowed_while_disabled(tmp_path):
    # Open a position first (file absent), then halt and confirm the EXIT still places.
    kf = tmp_path / "EXECUTOR_DISABLED"
    ex = Executor(_cfg(str(kf)), client=_StubClient())
    assert _fire(ex, ticker="T-A", wk=7)["placed"] is True
    kf.write_text("halted")                         # entries now blocked...
    assert _fire(ex, ticker="T-B", wk=7)["reason"] == "TRADING_DISABLED"
    out = ex.on_exit("T-A", window_key=7, exit_price_cents=60)
    assert out["placed"] is True                    # ...but the defensive close still runs


def test_empty_disable_file_path_is_inert():
    ex = Executor(_cfg(""), client=_StubClient())   # no path configured -> never disabled
    assert _fire(ex)["placed"] is True


def test_default_disable_file_path():
    assert ExecutorConfig().disable_file == "data/EXECUTOR_DISABLED"


# --------------------------------------------------------------------------- #
# the script toggles the file the executor reads
# --------------------------------------------------------------------------- #
def test_script_creates_and_removes_kill_file(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "disable_trading.sh")
    kf = tmp_path / "EXECUTOR_DISABLED"
    env = {**os.environ, "Q15_EXEC_DISABLE_FILE": str(kf)}

    # halt -> file exists and the executor refuses entries
    subprocess.run(["bash", script, "unit-test halt"], env=env, check=True,
                   capture_output=True, text=True)
    assert kf.exists()
    ex = Executor(_cfg(str(kf)), client=_StubClient())
    assert _fire(ex)["reason"] == "TRADING_DISABLED"

    # status reports halted
    st = subprocess.run(["bash", script, "--status"], env=env, check=True,
                        capture_output=True, text=True)
    assert "HALTED" in st.stdout

    # enable -> file gone and entries resume
    subprocess.run(["bash", script, "--enable"], env=env, check=True,
                   capture_output=True, text=True)
    assert not kf.exists()
    ex2 = Executor(_cfg(str(kf)), client=_StubClient())
    assert _fire(ex2)["placed"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
