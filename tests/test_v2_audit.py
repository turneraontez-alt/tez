"""Tests for the ultoim_v2 config-gate audit tool (``tools/v2_audit.py``).

Deterministic: a tiny hand-built SQLite ledger with a KNOWN set of rows whose NET
P&L is computable by hand, no network / clock / unseeded randomness. Verifies:
  1. the Kalshi fee formula on a known ask;
  2. gross-vs-net on a known winning NO at ask=70 (gross +30, net +28);
  3. a specific Gate predicate keeps/rejects the expected rows;
  4. the cumulative NO stack on the fixture yields the hand-computed mean (3.0);
  5. determinism — two audits with the same seed give identical CIs;
  6. ``--json`` output parses and has the expected top-level keys.

Mirrors the repo's ultoim_v2 test conventions (pure functions, tmp_path SQLite,
no live exchange / Telegram / worker timing).
"""
from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools import v2_audit


# --------------------------------------------------------------------------- #
# fixture — a tiny KNOWN ledger
# --------------------------------------------------------------------------- #
# All rows settle in window_key = 9000 // 900 = 10 (close_time 9000.0).
# A BTC row carries calibrated_yes=0.30 (decisively NO -> NO rows BTC-confirmed,
# YES rows not). Each NO row's NET P&L is hand-computed below.
#
# NO stack = inverse_edge_no & btc_confirm & ask_band & skip_7m_no.
#   r1: ask70 edge-3 win  10M -> KEPT  (net +28)
#   r2: ask60 edge-2 loss 10M -> KEPT  (net -62)
#   r3: ask55 edge-1 win  10M -> KEPT  (net +43)
#   r4: ask50 edge+5 win  10M -> dropped by inverse_edge_no (edge>=0)
#   r5: ask90 edge-4 win  10M -> dropped by ask_band (90>85)
#   r6: ask65 edge-2 win  7M  -> dropped by skip_7m_no (interval 7M)
# kept nets = [28, -62, 43] -> mean 3.0
CLOSE_TIME = 9000.0
WINDOW_KEY = 10

# (asset, interval, side, ask, edge, correct, cal_yes, mkt_yes)
_ROWS = [
    ("BTC", "10M", "NO", 30.0, -5.0, 1, 0.30, 0.30),   # BTC context row (self-confirms)
    ("ETH", "10M", "NO", 70.0, -3.0, 1, 0.40, 0.40),   # r1 KEPT
    ("SOL", "10M", "NO", 60.0, -2.0, 0, 0.45, 0.45),   # r2 KEPT
    ("XRP", "10M", "NO", 55.0, -1.0, 1, 0.42, 0.42),   # r3 KEPT
    ("DOGE", "10M", "NO", 50.0, 5.0, 1, 0.40, 0.40),   # r4 dropped: positive edge
    ("BNB", "10M", "NO", 90.0, -4.0, 1, 0.41, 0.41),   # r5 dropped: ask band
    ("HYPE", "7M", "NO", 65.0, -2.0, 1, 0.40, 0.40),   # r6 dropped: 7M
    # A YES row with high conviction (cal>=0.70, mkt>=0.60) — used for the
    # hiconv_yes gate test. BTC at 0.30 means YES is NOT btc-confirmed.
    ("ETH", "10M", "YES", 40.0, 4.0, 1, 0.75, 0.65),
]

_INSERT_COLS = (
    "created_at", "model_version", "asset", "ticker", "interval", "window_key",
    "fired", "research_fired", "predicted_side", "selected_probability",
    "calibrated_yes_probability", "market_implied_yes_probability",
    "net_edge_cents", "entry_ask_cents", "spread_cents", "close_time",
    "official_result", "correct", "hypothetical_pnl_cents",
)


def _make_ledger(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE ultoim_v2_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL, model_version TEXT, asset TEXT, ticker TEXT,
                interval TEXT, window_key INTEGER, fired INTEGER,
                research_fired INTEGER, predicted_side TEXT,
                selected_probability REAL, calibrated_yes_probability REAL,
                market_implied_yes_probability REAL, net_edge_cents REAL,
                entry_ask_cents REAL, spread_cents REAL, close_time REAL,
                official_result TEXT, correct INTEGER, hypothetical_pnl_cents REAL
            )
            """
        )
        for i, (asset, interval, side, ask, edge, correct, cal, mkt) in enumerate(_ROWS):
            official = "NO" if (side == "NO") == (correct == 1) else "YES"
            # gross hypothetical (deliberately fee-naive, to prove the tool ignores it)
            gross = (100.0 - ask) if correct else (-ask)
            conn.execute(
                f"INSERT INTO ultoim_v2_predictions ({','.join(_INSERT_COLS)}) "
                f"VALUES ({','.join('?' for _ in _INSERT_COLS)})",
                (
                    1000.0 + i,            # created_at (strictly increasing)
                    "ultoim-v2", asset, f"{asset}-T{i}", interval, WINDOW_KEY,
                    1 if side == "NO" else 0,    # fired (NO-only delivery)
                    1,                            # research_fired
                    side, 0.62, cal, mkt, edge, ask, 2.0, CLOSE_TIME,
                    official, correct, gross,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    p = tmp_path / "v2.sqlite3"
    _make_ledger(p)
    return p


@pytest.fixture()
def rows(ledger_path: Path):
    return v2_audit.load_rows(str(ledger_path))


# --------------------------------------------------------------------------- #
# 1. fee formula
# --------------------------------------------------------------------------- #
def test_fee_formula_matches_kalshi():
    # ceil(0.07 * P * (1-P) * 100) cents.
    assert v2_audit.kalshi_fee_cents(70) == math.ceil(0.07 * 0.70 * 0.30 * 100)
    assert v2_audit.kalshi_fee_cents(70) == 2
    assert v2_audit.kalshi_fee_cents(50) == math.ceil(0.07 * 0.50 * 0.50 * 100)
    assert v2_audit.kalshi_fee_cents(50) == 2
    # Boundaries are fee-free (no spread to tax).
    assert v2_audit.kalshi_fee_cents(0) == 0
    assert v2_audit.kalshi_fee_cents(100) == 0


# --------------------------------------------------------------------------- #
# 2. gross vs net — a winning NO at ask=70 is gross +30, net +28
# --------------------------------------------------------------------------- #
def test_gross_vs_net_winning_no_at_70():
    gross = 100.0 - 70.0
    assert gross == 30.0
    fee = v2_audit.kalshi_fee_cents(70)
    assert v2_audit.net_pnl_cents(70, correct=1) == gross - fee
    assert v2_audit.net_pnl_cents(70, correct=1) == 28.0
    # A loss pays the ask plus the fee.
    assert v2_audit.net_pnl_cents(70, correct=0) == -70.0 - fee
    assert v2_audit.net_pnl_cents(70, correct=0) == -72.0


# --------------------------------------------------------------------------- #
# 3. a specific Gate predicate keeps/rejects the expected rows
# --------------------------------------------------------------------------- #
def test_inverse_edge_no_gate_keeps_negative_edge_only(rows):
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext(rows))
    gate = v2_audit.GATES_BY_NAME["inverse_edge_no"]
    no_rows = [r for r in rows if v2_audit._side(r) == "NO"]
    kept = v2_audit.apply_gate(no_rows, gate, ctx)
    # The positive-edge DOGE row (edge +5) is rejected; everything negative is kept.
    kept_assets = {v2_audit._asset(r) for r in kept}
    assert "DOGE" not in kept_assets
    assert {"BTC", "ETH", "SOL", "XRP", "BNB", "HYPE"} <= kept_assets


def test_btc_confirm_gate_keeps_no_blocks_yes(rows):
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext(rows))
    gate = v2_audit.GATES_BY_NAME["btc_confirm"]
    # BTC cy=0.30 <= 0.35 -> NO confirmed; >= 0.60 needed for YES -> YES blocked.
    no_eth = next(r for r in rows if v2_audit._asset(r) == "ETH" and v2_audit._side(r) == "NO")
    yes_eth = next(r for r in rows if v2_audit._asset(r) == "ETH" and v2_audit._side(r) == "YES")
    assert gate.predicate(no_eth, ctx) is True
    assert gate.predicate(yes_eth, ctx) is False


def test_ask_band_gate_rejects_out_of_band(rows):
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext(rows))
    gate = v2_audit.GATES_BY_NAME["ask_band"]
    bnb = next(r for r in rows if v2_audit._asset(r) == "BNB")   # ask 90 > 85
    eth_no = next(r for r in rows if v2_audit._asset(r) == "ETH" and v2_audit._side(r) == "NO")
    assert gate.predicate(bnb, ctx) is False
    assert gate.predicate(eth_no, ctx) is True


def test_btc_fail_open_when_no_context():
    # A NO row with no matching BTC context -> fail-open (confirmed/True).
    row = {"asset": "ETH", "predicted_side": "NO", "close_time": 1.0,
           "window_key": 999, "interval": "10M"}
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext([]))
    gate = v2_audit.GATES_BY_NAME["btc_confirm"]
    assert gate.predicate(row, ctx) is True


# --------------------------------------------------------------------------- #
# 4. the cumulative NO stack yields the hand-computed mean (3.0)
# --------------------------------------------------------------------------- #
def test_cumulative_no_stack_mean(rows):
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext(rows))
    settled = [r for r in rows if r.get("correct") is not None]
    no_rows = v2_audit.side_population(settled, "NO")
    kept = v2_audit.apply_stack(no_rows, v2_audit.NO_STACK, ctx)
    kept_assets = sorted(v2_audit._asset(r) for r in kept)
    # Exactly r1(ETH), r2(SOL), r3(XRP) survive the conjunction.
    assert kept_assets == ["ETH", "SOL", "XRP"]
    nets = sorted(v2_audit.net_pnls(kept))
    assert nets == [-62.0, 28.0, 43.0]
    assert sum(nets) / len(nets) == pytest.approx(3.0)


def test_hiconv_yes_stack_keeps_high_conviction(rows):
    ctx = v2_audit.AuditContext(btc=v2_audit.BtcContext(rows))
    yes_rows = [r for r in rows if v2_audit._side(r) == "YES"]
    gate = v2_audit.GATES_BY_NAME["hiconv_yes"]
    kept = v2_audit.apply_gate(yes_rows, gate, ctx)
    # The single YES row has cal=0.75>=0.70 AND mkt=0.65>=0.60 -> kept.
    assert len(kept) == 1
    assert v2_audit._asset(kept[0]) == "ETH"


# --------------------------------------------------------------------------- #
# 5. determinism — same seed -> identical CIs
# --------------------------------------------------------------------------- #
def test_determinism_same_seed_identical_cis(rows):
    a = v2_audit.run_audit(rows, side="BOTH", seed=1234, resamples=500)
    b = v2_audit.run_audit(rows, side="BOTH", seed=1234, resamples=500)
    assert a["cumulative_stacks"]["NO"]["overall"] == b["cumulative_stacks"]["NO"]["overall"]
    assert a["per_gate_marginals"] == b["per_gate_marginals"]
    assert a["headline_verdicts"] == b["headline_verdicts"]


def test_bootstrap_ci_brackets_mean(rows):
    rng = __import__("random").Random("fixed")
    out = v2_audit.bootstrap_mean_ci([28.0, -62.0, 43.0], rng, resamples=1000)
    assert out["mean"] == pytest.approx(3.0)
    assert out["ci_low"] <= out["mean"] <= out["ci_high"]


def test_percentile_helper():
    vals = [1.0, 2.0, 3.0, 4.0]
    assert v2_audit.percentile(vals, 0) == 1.0
    assert v2_audit.percentile(vals, 100) == 4.0
    assert v2_audit.percentile(vals, 50) == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# 6. --json output parses and has the expected top-level keys
# --------------------------------------------------------------------------- #
def test_json_output_has_expected_keys(ledger_path: Path):
    proc = subprocess.run(
        [sys.executable, "tools/v2_audit.py", "--db", str(ledger_path),
         "--json", "--resamples", "200"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    expected = {"config", "populations", "per_gate_marginals", "cumulative_stacks",
                "oos_robustness", "headline_verdicts", "seed", "resamples",
                "settled_total", "db_path"}
    assert expected <= set(data.keys())
    # Config is auto-reflected from the live dataclass: it must carry known fields.
    field_names = {f["name"] for f in data["config"]["fields"]}
    assert {"btc_confirm_enabled", "require_inverse_edge", "ask_lo"} <= field_names
    # Each gate in the registry surfaces in the marginals.
    marg_names = {g["name"] for g in data["per_gate_marginals"]}
    assert {g.name for g in v2_audit.GATES} == marg_names


def test_config_reflection_is_auto():
    out = v2_audit.reflect_config()
    assert out["class"] == "UltoimV2Config"
    import dataclasses

    from q15_upgrade.ultoim_v2.config import UltoimV2Config
    # Every dataclass field is reflected — zero-edit extensibility.
    reflected = {f["name"] for f in out["fields"]}
    declared = {f.name for f in dataclasses.fields(UltoimV2Config)}
    assert reflected == declared


def test_headline_verdict_tags(rows):
    res = v2_audit.run_audit(rows, side="BOTH", seed=1234, resamples=300)
    verdicts = res["headline_verdicts"]
    assert set(verdicts.keys()) == {"NO", "YES"}
    for sd in ("NO", "YES"):
        assert verdicts[sd]["tag"] in ("PASS", "MARGINAL", "FAIL")
