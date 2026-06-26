"""Deterministic tests for the read-only V2 (ultoim_v2) account auditor.

No network, no live exchanges, no wall-clock dependence. Each test builds its own
temp SQLite ledgers (via the REAL ``UltoimV2Ledger`` / ``IntervalResearchLedger``
so the schema and stored money math are exactly the production ones), then runs
the auditor read-only. The tampered-row cases write a single column directly to
prove the consistency checks fire.

Covers the full spec test matrix:
  (1) P&L reconcile match + injected corrupted-stored-pnl flagged;
  (2) EV formula NO and YES and stake=2 (both EV forms agree);
  (3) canonical dedup (10M+7M → one trade, 10M chosen; 7M-only → 7M);
  (4) no-look-ahead (10M=NO graded as NO even when 7M flips to YES);
  (5) consistency check catches tampered correct/side/result;
  (6) before/after diff (new, newly-resolved, regrade-flip with old→new);
  (7) chart extension via the REAL gate (passing included, failing excluded, dedup);
  (8) thin data → INSUFFICIENT; empty/missing DB → available False, no crash;
  (9) account rollup arithmetic exact on a hand-checked fixture.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.gate import evaluate as gate_evaluate
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger
from q15_upgrade.interval_research.ledger import IntervalResearchLedger

from tools import v2_audit


MODEL = "ultoim-v2"
GRADE = ("10M", "7M")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _row(ticker, interval, window_key, *, side="NO", ask=60.0, cal_yes=0.40,
         net_edge=3.0, fired=1, close_time=1000.0, asset=None, stake=1,
         total_cost=2.0, spread=2.0, distance=1.2, delivery_status="SENT"):
    """A minimal but schema-valid V2 prediction row for record_decision()."""
    return {
        "created_at": 1.0,
        "model_version": MODEL,
        "asset": asset or ticker.split("-")[0] if "-" in ticker else (asset or ticker),
        "ticker": ticker,
        "interval": interval,
        "window_key": window_key,
        "fired": fired,
        "predicted_side": side,
        "selected_probability": (cal_yes if side == "YES" else 1.0 - cal_yes),
        "calibrated_yes_probability": cal_yes,
        "conservative_probability": 0.5,
        "net_edge_cents": net_edge,
        "entry_ask_cents": ask,
        "total_cost_cents": total_cost,
        "spread_cents": spread,
        "distance_sigma": distance,
        "close_time": close_time,
        "stake_multiplier": stake,
        "gate_b_pass": 1,
        "gate_c_pass": 1,
        "delivery_status": delivery_status,
        "session_id": "sess-1",
    }


def _build_v2(tmp_path, rows, *, resolutions=None, name="v2.sqlite3"):
    """Create a V2 ledger, record rows, optionally resolve tickers, return path."""
    path = str(tmp_path / name)
    led = UltoimV2Ledger(path)
    for r in rows:
        # Inject a defaulted asset for grouping clarity.
        r.setdefault("asset", r["ticker"])
        led.record_decision(r)
    if resolutions:
        for ticker, result in resolutions.items():
            led.resolve(MODEL, ticker, result, now=2000.0)
    led.close()
    return path


def _build_captures(tmp_path, captures, *, resolutions=None, name="captures.sqlite3",
                    model_version=MODEL):
    path = str(tmp_path / name)
    led = IntervalResearchLedger(path)
    for c in captures:
        led.record_capture(c)
    if resolutions:
        for ticker, result in resolutions.items():
            led.resolve(model_version, ticker, result, resolved_at=2000.0)
    led.close = lambda: None  # IntervalResearchLedger has no close(); no-op guard
    return path


def _capture_row(ticker, interval, window_key, *, side="NO", ask=60.0, cal_yes=0.40,
                 total_cost=2.0, net_edge=3.0, distance=1.2, model_version=MODEL):
    return {
        "model_version": model_version,
        "ticker": ticker,
        "asset": ticker,
        "interval": interval,
        "window_key": window_key,
        "close_time": 1000.0,
        "prediction_available": 1,
        "predicted_side": side,
        "calibrated_yes_probability": cal_yes,
        "conservative_probability": 0.5,
        "distance_from_strike": distance,
        "entry_ask_cents": ask,
        "net_edge_cents": net_edge,
        "total_cost_cents": total_cost,
        "spread_cents": 2.0,
    }


def _hermetic_cfg():
    """A config built from dataclass DEFAULTS, NOT from_env(), so the replayed gate
    rules are fixed regardless of any ambient Q15_ULTOIM_V2_* env var the host
    exports (deterministic tests — guideline: no ambient/environmental state)."""
    return UltoimV2Config()


def _audit(db_path, captures_path, tmp_path, *, extend=False, min_n=30,
           snapshot=None, strict_c5=False, captures_model=MODEL,
           delivered_only=False, per_window=False, flag_asset=""):
    return v2_audit.run_audit(
        db_path=db_path,
        captures_path=captures_path or str(tmp_path / "missing_captures.sqlite3"),
        model_version=MODEL,
        captures_model_version=captures_model,
        grade_intervals=GRADE,
        min_n=min_n,
        snapshot_path=snapshot or str(tmp_path / "snap.json"),
        extend=extend,
        delivered_only=delivered_only,
        per_window=per_window,
        flag_asset=flag_asset,
        cfg=_hermetic_cfg() if extend else None,
        gate_evaluate=gate_evaluate if extend else None,
        strict_c5=strict_c5,
    )


def _trade(report, ticker, window_key):
    for t in report["trades"]:
        if t["ticker"] == ticker and t["window_key"] == window_key:
            return t
    raise AssertionError(f"trade {ticker}/{window_key} not found")


# --------------------------------------------------------------------------- #
# (1) P&L reconcile match + corrupted-stored flagged
# --------------------------------------------------------------------------- #
def test_pnl_reconcile_match(tmp_path):
    # NO @ ask 60, settles NO -> win -> +40. resolve() stores it; recompute mirrors.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert t["resolved"] is True
    assert t["stored_pnl_cents"] == pytest.approx(40.0)
    assert t["recomputed_pnl_cents"] == pytest.approx(40.0)
    assert t["side_matches_result"] is True
    assert "PNL_STORED_MISMATCH" not in t["flags"]
    assert rep["discrepancies"] == []


def test_corrupted_stored_pnl_flagged(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    # Tamper the stored P&L directly (a corrupted value other tools would trust).
    conn = sqlite3.connect(db)
    conn.execute("UPDATE ultoim_v2_predictions SET hypothetical_pnl_cents=999.0 "
                 "WHERE ticker='BTC-A'")
    conn.commit()
    conn.close()
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert "PNL_STORED_MISMATCH" in t["flags"]
    assert t["recomputed_pnl_cents"] == pytest.approx(40.0)  # mirror is still right
    keys = {d["key"] for d in rep["discrepancies"]}
    assert t["key"] in keys


# --------------------------------------------------------------------------- #
# (2) EV formula NO / YES / stake=2
# --------------------------------------------------------------------------- #
def test_ev_no_side():
    # NO @ ask 60, calibrated_yes 0.40 -> p_win = 0.60. EV = 0.60*100 - 60 = 0.
    ev = v2_audit.compute_ev("NO", Decimal("0.40"), Decimal("60"), Decimal("1"))
    assert ev == Decimal("0")


def test_ev_yes_side():
    # YES @ ask 55, calibrated_yes 0.70 -> p_win=0.70. EV=0.70*100-55=15.
    ev = v2_audit.compute_ev("YES", Decimal("0.70"), Decimal("55"), Decimal("1"))
    assert ev == Decimal("15")


def test_ev_stake_two_and_forms_agree():
    # stake=2 doubles EV; both Decimal forms must be bit-identical.
    ev = v2_audit.compute_ev("NO", Decimal("0.65"), Decimal("60"), Decimal("2"))
    # p_win=0.35 -> (0.35*100-60)*2 = (35-60)*2 = -50
    assert ev == Decimal("-50")
    # explicit cross-check of the two algebraic forms
    p = Decimal("0.35")
    a = Decimal("60")
    s = Decimal("2")
    form1 = (p * Decimal("100") - a) * s
    form2 = (p * (Decimal("100") - a) + (Decimal("1") - p) * (-a)) * s
    assert form1 == form2 == ev


def test_ev_missing_data_none():
    assert v2_audit.compute_ev("NO", None, Decimal("60"), Decimal("1")) is None
    assert v2_audit.compute_ev("NO", Decimal("0.4"), None, Decimal("1")) is None
    assert v2_audit.compute_ev("", Decimal("0.4"), Decimal("60"), Decimal("1")) is None


# --------------------------------------------------------------------------- #
# (3) canonical dedup
# --------------------------------------------------------------------------- #
def test_dedup_10m_and_7m_one_trade_10m_chosen(tmp_path):
    rows = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("BTC-A", "7M", 100, side="NO", ask=65.0),
    ]
    db = _build_v2(tmp_path, rows, resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    btc = [t for t in rep["trades"] if t["ticker"] == "BTC-A"]
    assert len(btc) == 1  # never double-count the 15-minute window
    assert btc[0]["interval"] == "10M"
    assert btc[0]["ask_cents"] == pytest.approx(60.0)
    assert "MULTI_INTERVAL_WINDOW" in btc[0]["flags"]


def test_dedup_divergent_window_keys_one_trade(tmp_path):
    # A single contract whose 10M and 7M rows carry DIFFERENT stored window_keys
    # (a live-path artifact: _window_key falls back to int(now//900) when
    # close_time is None and the two cycles straddle a 900s boundary). It must
    # collapse to exactly ONE canonical trade for the ticker — never split into
    # two and double-count the 15-minute window — with the divergence flagged.
    rows = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("BTC-A", "7M", 101, side="NO", ask=65.0),
    ]
    db = _build_v2(tmp_path, rows, resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    btc = [t for t in rep["trades"] if t["ticker"] == "BTC-A"]
    assert len(btc) == 1  # ONE trade despite divergent window_keys
    assert btc[0]["interval"] == "10M"  # 10M still wins by priority
    assert "WINDOW_KEY_DIVERGENCE" in btc[0]["flags"]
    # Account P&L is the single +40 (NO @ 60 wins), NOT +80 (double-count).
    assert rep["account"]["overall"]["pnl_total_cents"] == pytest.approx(40.0)
    assert rep["account"]["overall"]["n"] == 1


def test_dedup_7m_only_chosen(tmp_path):
    rows = [_row("ETH-A", "7M", 200, side="NO", ask=70.0)]
    db = _build_v2(tmp_path, rows, resolutions={"ETH-A": "NO"})
    rep = _audit(db, None, tmp_path)
    eth = [t for t in rep["trades"] if t["ticker"] == "ETH-A"]
    assert len(eth) == 1
    assert eth[0]["interval"] == "7M"


def test_research_only_intervals_not_delivered(tmp_path):
    # A 12M fired row must NOT become a canonical trade (research-only mark).
    rows = [_row("SOL-A", "12M", 300, side="NO", ask=60.0)]
    db = _build_v2(tmp_path, rows, resolutions={"SOL-A": "NO"})
    rep = _audit(db, None, tmp_path)
    assert [t for t in rep["trades"] if t["ticker"] == "SOL-A"] == []


# --------------------------------------------------------------------------- #
# (4) no look-ahead
# --------------------------------------------------------------------------- #
def test_no_look_ahead_grades_canonical_only(tmp_path):
    # 10M says NO, 7M flips to YES. Window settles YES. The 10M canonical trade
    # must be graded WRONG (NO lost), never re-graded using the 7M flip.
    rows = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("BTC-A", "7M", 100, side="YES", ask=45.0, cal_yes=0.70),
    ]
    db = _build_v2(tmp_path, rows, resolutions={"BTC-A": "YES"})
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert t["interval"] == "10M"
    assert t["predicted_side"] == "NO"
    assert t["side_matches_result"] is False  # NO vs YES -> wrong
    # P&L: NO @ ask 60 lost -> -60.
    assert t["recomputed_pnl_cents"] == pytest.approx(-60.0)


# --------------------------------------------------------------------------- #
# (5) consistency checks catch tampering
# --------------------------------------------------------------------------- #
def test_tampered_correct_flagged(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    conn = sqlite3.connect(db)
    # Settled NO with side NO is correct=1; force it to 0.
    conn.execute("UPDATE ultoim_v2_predictions SET correct=0 WHERE ticker='BTC-A'")
    conn.commit()
    conn.close()
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert "CORRECT_MISMATCH" in t["flags"]


def test_closed_but_unresolved_flagged(tmp_path):
    # Fired row with a close_time but never resolved -> CLOSED_BUT_UNRESOLVED.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0,
                                   close_time=1000.0)])
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert t["resolved"] is False
    assert "CLOSED_BUT_UNRESOLVED" in t["flags"]


def test_missing_ask_on_resolved_flagged(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=None)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert "MISSING_ASK" in t["flags"]
    assert t["recomputed_pnl_cents"] is None


# --------------------------------------------------------------------------- #
# (6) before / after diff
# --------------------------------------------------------------------------- #
def test_before_after_diff(tmp_path):
    snap_path = str(tmp_path / "snap.json")
    # First run: BTC-A resolved NO, SOL-A still OPEN (unresolved) -> baseline.
    rows1 = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("SOL-A", "10M", 102, side="NO", ask=70.0, close_time=1000.0),
    ]
    db1 = _build_v2(tmp_path, rows1, resolutions={"BTC-A": "NO"}, name="v2a.sqlite3")
    rep1 = _audit(db1, None, tmp_path, snapshot=snap_path)
    v2_audit.save_snapshot(snap_path, rep1["snapshot"])
    assert rep1["changes_since_last_audit"]["has_prior"] is False

    # Second run: BTC-A regrades to wrong (settles YES now), SOL-A newly resolved,
    # ETH-A is a brand-new trade.
    rows2 = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),  # will settle YES now
        _row("ETH-A", "10M", 101, side="NO", ask=55.0),  # new, unresolved
        _row("SOL-A", "10M", 102, side="NO", ask=70.0),  # newly resolved
    ]
    db2 = _build_v2(tmp_path, rows2, resolutions={"BTC-A": "YES", "SOL-A": "NO"},
                    name="v2b.sqlite3")
    rep2 = _audit(db2, None, tmp_path, snapshot=snap_path)
    diff = rep2["changes_since_last_audit"]
    assert diff["has_prior"] is True

    new_keys = {t["ticker"] for t in diff["new_trades"]}
    assert "ETH-A" in new_keys
    assert "SOL-A" not in new_keys  # SOL-A existed in the prior snapshot

    resolved_keys = {t["ticker"] for t in diff["newly_resolved"]}
    assert "SOL-A" in resolved_keys

    regraded = {r["ticker"]: r for r in diff["regraded"]}
    assert "BTC-A" in regraded
    assert regraded["BTC-A"]["old_correct"] is True
    assert regraded["BTC-A"]["new_correct"] is False

    pnl_changed = {c["ticker"]: c for c in diff["pnl_changed"]}
    assert "BTC-A" in pnl_changed
    assert pnl_changed["BTC-A"]["old_pnl_cents"] == pytest.approx(40.0)
    assert pnl_changed["BTC-A"]["new_pnl_cents"] == pytest.approx(-60.0)


def test_before_after_idempotent_rerun_empty_diff(tmp_path):
    # The lens's core guarantee: a deterministic re-run against the tool's OWN
    # freshly-saved snapshot yields an EMPTY diff and byte-stable snapshot output.
    snap_path = str(tmp_path / "snap.json")
    rows = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("ETH-A", "10M", 101, side="NO", ask=70.0),
        _row("SOL-A", "10M", 102, side="NO", ask=55.0),
    ]
    db = _build_v2(tmp_path, rows,
                   resolutions={"BTC-A": "NO", "ETH-A": "YES", "SOL-A": "NO"})
    rep1 = _audit(db, None, tmp_path, snapshot=snap_path)
    v2_audit.save_snapshot(snap_path, rep1["snapshot"])

    rep2 = _audit(db, None, tmp_path, snapshot=snap_path)
    diff = rep2["changes_since_last_audit"]
    assert diff["has_prior"] is True
    assert diff["new_trades"] == []
    assert diff["newly_resolved"] == []
    assert diff["regraded"] == []
    assert diff["pnl_changed"] == []
    assert diff["removed_trades"] == []

    # Snapshot output is byte-stable across the two runs.
    import json
    assert (json.dumps(rep1["snapshot"], sort_keys=True, default=str)
            == json.dumps(rep2["snapshot"], sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# C5 config fingerprint
# --------------------------------------------------------------------------- #
def test_c5_fingerprint_strict_when_env_clean(tmp_path, monkeypatch):
    # Empty Q15_ULTOIM_V2_* env -> from_env() reproduces strict C5.
    for k in [k for k in list(__import__("os").environ) if k.startswith("Q15_ULTOIM_V2_")]:
        monkeypatch.delenv(k, raising=False)
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    assert rep["is_strict_c5"] is True
    assert rep["config_divergences"] == []
    assert rep["snapshot"]["is_strict_c5"] is True
    assert rep["snapshot"]["config_fingerprint"]["min_confidence"] == pytest.approx(0.55)


def test_c5_fingerprint_reports_divergence(tmp_path, monkeypatch):
    # Inject one divergent env -> is_strict_c5 False AND the divergence is reported.
    for k in [k for k in list(__import__("os").environ) if k.startswith("Q15_ULTOIM_V2_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("Q15_ULTOIM_V2_REQUIRE_INVERSE_EDGE", "false")
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path)
    assert rep["is_strict_c5"] is False
    fields = {d["field"] for d in rep["config_divergences"]}
    assert "require_inverse_edge" in fields
    # The text report surfaces the divergence loudly.
    txt = v2_audit.render_report(rep)
    assert "NOT STRICT C5" in txt
    assert "require_inverse_edge" in txt


def test_c5_strict_flag_overrides_env(tmp_path, monkeypatch):
    # --strict-c5 builds cfg from the pins regardless of the divergent env.
    for k in [k for k in list(__import__("os").environ) if k.startswith("Q15_ULTOIM_V2_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("Q15_ULTOIM_V2_REQUIRE_INVERSE_EDGE", "false")
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path, strict_c5=True)
    assert rep["is_strict_c5"] is True
    assert rep["config_divergences"] == []
    assert rep["config_fingerprint"]["require_inverse_edge"] is True


# --------------------------------------------------------------------------- #
# (7) chart extension via the REAL gate
# --------------------------------------------------------------------------- #
def test_chart_extension_includes_passing_excludes_failing(tmp_path):
    # V2 delivered nothing. Two chart windows: one a clean NO that the current
    # gate fires, one a YES with no BTC confirmation that the gate blocks.
    db = _build_v2(tmp_path, [], name="v2empty_butrow.sqlite3")  # empty -> not available
    # Need at least one V2 row so the audit is "available"; make it a different
    # window than the chart extension candidates.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 50, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"}, name="v2c.sqlite3")

    captures = [
        # Expensive NO @ 75 on 10M (in the (ask_hi, expensive_no_ask_hi] band) with
        # sel = 1 - 0.38 = 0.62 >= min_confidence -> the current gate's expensive-NO
        # admit FIRES it (btc_confirm fails-open on btc_yes_prob=None).
        _capture_row("ETH-A", "10M", 60, side="NO", ask=75.0, cal_yes=0.38,
                     net_edge=-2.0, distance=1.2),
        # Low-confidence YES: sel = cal_yes = 0.52 < min_confidence (0.55) -> the
        # confidence floor excludes it deterministically.
        _capture_row("SOL-A", "10M", 61, side="YES", ask=80.0, cal_yes=0.52,
                     net_edge=-5.0, distance=1.0),
    ]
    cap = _build_captures(tmp_path, captures,
                          resolutions={"ETH-A": "NO", "SOL-A": "YES"})
    rep = _audit(db, cap, tmp_path, extend=True)
    ce = rep["chart_extension"]
    assert ce["available"] is True
    tickers = {t["ticker"] for t in ce["trades"]}
    assert "ETH-A" in tickers       # passed the current gate
    assert "SOL-A" not in tickers   # failed (sub-confidence YES)
    # dedup: one would-have per window.
    assert len(ce["trades"]) == len({(t["ticker"], t["window_key"]) for t in ce["trades"]})


def test_chart_extension_lower_priority_valid_when_higher_missing(tmp_path):
    # A window V2 did NOT deliver whose 10M capture is a MISSING row
    # (predicted_side NULL via record_missing) but whose 7M capture is a valid
    # would-have NO. The higher-priority abstain must NOT mask the valid
    # lower-priority would-have: the 7M row is evaluated and fires.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 50, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"}, name="v2miss.sqlite3")
    cap_path = str(tmp_path / "captures_miss.sqlite3")
    led = IntervalResearchLedger(cap_path)
    # 10M missing (no prediction); 7M a clean expensive-NO that fires.
    led.record_missing(model_version=MODEL, ticker="ETH-A", asset="ETH-A",
                       interval="10M", reason="NO_PREDICTION", window_key=60,
                       captured_at=1000.0)
    led.record_capture(_capture_row("ETH-A", "7M", 60, side="NO", ask=75.0,
                                    cal_yes=0.38, net_edge=-2.0, distance=1.2))
    led.resolve(MODEL, "ETH-A", "NO", resolved_at=2000.0)
    rep = _audit(db, cap_path, tmp_path, extend=True)
    ce = rep["chart_extension"]
    eth = [t for t in ce["trades"] if t["ticker"] == "ETH-A"]
    assert len(eth) == 1
    assert eth[0]["interval"] == "7M"  # the valid lower-priority interval, not the missing 10M


def test_chart_extension_skips_delivered_windows(tmp_path):
    # If V2 already delivered a (ticker, window), the chart row for it is not an
    # extension even if it would fire.
    db = _build_v2(tmp_path, [_row("ETH-A", "10M", 60, side="NO", ask=60.0)],
                   resolutions={"ETH-A": "NO"}, name="v2d.sqlite3")
    captures = [_capture_row("ETH-A", "10M", 60, side="NO", ask=60.0)]
    cap = _build_captures(tmp_path, captures, resolutions={"ETH-A": "NO"})
    rep = _audit(db, cap, tmp_path, extend=True)
    assert [t for t in rep["chart_extension"]["trades"] if t["ticker"] == "ETH-A"] == []


# --------------------------------------------------------------------------- #
# (8) thin data / empty DB
# --------------------------------------------------------------------------- #
def test_thin_data_insufficient(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path, min_n=30)
    assert rep["account"]["overall"]["insufficient"] is True
    assert rep["account"]["overall"]["n"] == 1  # raw count preserved, not laundered


def test_missing_db_available_false(tmp_path):
    rep = _audit(str(tmp_path / "does_not_exist.sqlite3"), None, tmp_path)
    assert rep["available"] is False
    assert "not found" in rep["reason"].lower() or "no rows" in rep["reason"].lower()


def test_empty_db_available_false(tmp_path):
    db = _build_v2(tmp_path, [])  # schema created, no rows
    rep = _audit(db, None, tmp_path)
    assert rep["available"] is False


def test_render_does_not_crash_on_unavailable(tmp_path):
    rep = _audit(str(tmp_path / "nope.sqlite3"), None, tmp_path)
    txt = v2_audit.render_report(rep)
    assert "unavailable" in txt.lower()


# --------------------------------------------------------------------------- #
# (9) account rollup arithmetic exact on a hand-checked fixture
# --------------------------------------------------------------------------- #
def test_account_rollup_exact(tmp_path):
    # Three NO trades on 10M:
    #   BTC @ ask 60 settles NO  -> win  -> +40, staked 60
    #   ETH @ ask 70 settles YES -> loss -> -70, staked 70
    #   SOL @ ask 55 settles NO  -> win  -> +45, staked 55
    # Totals: P&L = 40 - 70 + 45 = +15; staked = 185; ROI = 15/185.
    # win rate = 2/3.
    rows = [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0),
        _row("ETH-A", "10M", 101, side="NO", ask=70.0),
        _row("SOL-A", "10M", 102, side="NO", ask=55.0),
    ]
    db = _build_v2(tmp_path, rows,
                   resolutions={"BTC-A": "NO", "ETH-A": "YES", "SOL-A": "NO"})
    rep = _audit(db, None, tmp_path, min_n=1)
    overall = rep["account"]["overall"]
    assert overall["n"] == 3
    assert overall["right"] == 2
    assert overall["wrong"] == 1
    assert overall["pnl_total_cents"] == pytest.approx(15.0)
    assert overall["staked_cents"] == pytest.approx(185.0)
    assert overall["roi"] == pytest.approx(15.0 / 185.0)
    assert overall["win_rate"] == pytest.approx(2.0 / 3.0)
    assert overall["insufficient"] is False
    # by_interval rollup matches overall (all 10M).
    assert rep["account"]["by_interval"]["10M"]["pnl_total_cents"] == pytest.approx(15.0)
    # by_side: all NO.
    assert rep["account"]["by_side"]["NO"]["n"] == 3
    assert rep["account"]["by_side"]["YES"]["n"] == 0


def test_ev_aggregate_and_edge_realization(tmp_path):
    # NO @ ask 60, cal_yes 0.40 -> p_win 0.60 -> EV = 0. Settles NO -> +40.
    # edge realization = 40 - 0 = 40.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0,
                                   cal_yes=0.40)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path, min_n=1)
    overall = rep["account"]["overall"]
    assert overall["ev_total_cents"] == pytest.approx(0.0)
    assert overall["edge_realization_cents"] == pytest.approx(40.0)
    t = _trade(rep, "BTC-A", 100)
    assert t["ev_cents"] == pytest.approx(0.0)
    assert t["edge_realization_cents"] == pytest.approx(40.0)


# --------------------------------------------------------------------------- #
# chart-vs-ledger per-unit cross-check + stake handling
# --------------------------------------------------------------------------- #
def test_chart_unit_pnl_cross_check_consistent(tmp_path):
    # Same trade present in both ledgers, both settle NO -> chart unit pnl = +40,
    # ledger per-unit = +40 -> no CHART_PNL_MISMATCH.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    captures = [_capture_row("BTC-A", "10M", 100, side="NO", ask=60.0)]
    cap = _build_captures(tmp_path, captures, resolutions={"BTC-A": "NO"})
    rep = _audit(db, cap, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert t["chart_unit_pnl_cents"] == pytest.approx(40.0)
    assert "CHART_PNL_MISMATCH" not in t["flags"]


def test_chart_unit_pnl_mismatch_flagged(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    captures = [_capture_row("BTC-A", "10M", 100, side="NO", ask=60.0)]
    cap = _build_captures(tmp_path, captures, resolutions={"BTC-A": "NO"})
    # Corrupt the chart realized pnl.
    conn = sqlite3.connect(cap)
    conn.execute("UPDATE interval_captures SET realized_pnl_cents=12.5 "
                 "WHERE ticker='BTC-A'")
    conn.commit()
    conn.close()
    rep = _audit(db, cap, tmp_path)
    t = _trade(rep, "BTC-A", 100)
    assert "CHART_PNL_MISMATCH" in t["flags"]


# --------------------------------------------------------------------------- #
# captures live under their OWN model_version namespace (regression: the V2
# ledger is "ultoim-v2" but interval_captures default to "interval-research-v1";
# querying captures with the V2 model_version silently matched ZERO rows, which
# no-op'd BOTH the chart cross-check and the chart-replay extension).
# --------------------------------------------------------------------------- #
def test_captures_distinct_model_version_namespace(tmp_path):
    assert v2_audit.DEFAULT_CAPTURES_MODEL_VERSION == "interval-research-v1"
    # V2 ledger under "ultoim-v2"; captures under the DISTINCT "interval-research-v1".
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    cap = _build_captures(
        tmp_path,
        [_capture_row("BTC-A", "10M", 100, side="NO", ask=60.0,
                      model_version="interval-research-v1")],
        resolutions={"BTC-A": "NO"}, model_version="interval-research-v1")
    # Correct namespace (the real default) -> captures match and the chart leg engages.
    rep = _audit(db, cap, tmp_path, captures_model="interval-research-v1")
    assert rep["captures_rows_matched"] == 1
    t = _trade(rep, "BTC-A", 100)
    assert t["chart_unit_pnl_cents"] == pytest.approx(40.0)
    assert "CHART_PNL_MISMATCH" not in t["flags"]
    # Wrong namespace (the old bug: querying captures with the V2 model_version)
    # matches zero rows -> the chart cross-check silently never runs.
    rep_wrong = _audit(db, cap, tmp_path, captures_model=MODEL)
    assert rep_wrong["captures_rows_matched"] == 0
    assert _trade(rep_wrong, "BTC-A", 100)["chart_unit_pnl_cents"] is None


def test_chart_extension_uses_captures_namespace(tmp_path):
    # A window V2 did NOT deliver, present only in the captures store under the
    # real "interval-research-v1" namespace, still surfaces as a would-have trade.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    cap = _build_captures(
        tmp_path,
        [_capture_row("ETH-A", "10M", 60, side="NO", ask=75.0, cal_yes=0.38,
                      net_edge=-3.0, model_version="interval-research-v1")],
        resolutions={"ETH-A": "NO"}, model_version="interval-research-v1")
    rep = _audit(db, cap, tmp_path, extend=True, captures_model="interval-research-v1")
    ext = rep["chart_extension"]
    assert ext["available"] is True
    assert ext["trade_count"] >= 1
    assert any(t["ticker"] == "ETH-A" for t in ext["trades"])


# --------------------------------------------------------------------------- #
# delivered-only population + per-window account + flagged-asset drag
# (owner: "only one buy counts per 15 min" and "keep HYPE but flag it")
# --------------------------------------------------------------------------- #
def test_delivered_only_restricts_to_sent(tmp_path):
    db = _build_v2(tmp_path, [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0, delivery_status="SENT"),
        _row("ETH-A", "10M", 100, side="NO", ask=60.0, delivery_status="RECORDED"),
    ], resolutions={"BTC-A": "NO", "ETH-A": "NO"})
    full = _audit(db, None, tmp_path)
    deliv = _audit(db, None, tmp_path, delivered_only=True)
    assert full["trade_count"] == 2 and full["population"] == "fired"
    assert deliv["trade_count"] == 1 and deliv["population"] == "delivered"
    assert deliv["trades"][0]["ticker"] == "BTC-A"


def test_per_window_account_one_buy_per_window(tmp_path):
    # Two assets settle in the SAME 15-min window (window_key=100): one buy counts.
    # BTC NO wins (settles NO), ETH NO loses (settles YES). Different asks/probs so
    # the selection rules diverge.
    db = _build_v2(tmp_path, [
        _row("BTC-A", "10M", 100, side="NO", ask=62.0, cal_yes=0.30),  # high conf NO, wins
        _row("ETH-A", "10M", 100, side="NO", ask=55.0, cal_yes=0.45),  # cheaper, lower conf, loses
        _row("SOL-A", "10M", 101, side="NO", ask=70.0, cal_yes=0.20),  # other window, wins
    ], resolutions={"BTC-A": "NO", "ETH-A": "YES", "SOL-A": "NO"})
    rep = _audit(db, None, tmp_path, per_window=True, min_n=1)
    pw = rep["per_window_account"]
    assert pw["windows"] == 2          # two distinct 15-min windows, not 3 trades
    # best-case takes the winner in window 100 (BTC +38) + SOL (+30) = +68
    assert pw["best_case"]["pnl_total_cents"] == pytest.approx(68.0)
    # worst-case takes the loser in window 100 (ETH -55) + SOL (+30) = -25
    assert pw["worst_case"]["pnl_total_cents"] == pytest.approx(-25.0)
    # highest-confidence in window 100 is BTC (p_win 0.70 > 0.55) -> winner
    assert pw["highest_confidence"]["pnl_total_cents"] == pytest.approx(68.0)
    # cheapest ask in window 100 is ETH (55<62) -> the loser
    assert pw["cheapest_ask"]["pnl_total_cents"] == pytest.approx(-25.0)
    assert pw["best_case"]["n"] == 2 and pw["worst_case"]["n"] == 2


def test_flagged_asset_drag_always_shown(tmp_path):
    db = _build_v2(tmp_path, [
        _row("HYPE-A", "10M", 100, side="NO", ask=70.0),   # loses (settles YES)
        _row("BTC-A", "10M", 101, side="NO", ask=60.0),    # wins
    ], resolutions={"HYPE-A": "YES", "BTC-A": "NO"})
    rep = _audit(db, None, tmp_path, flag_asset="HYPE", min_n=1)
    af = rep["asset_flag"]
    assert af["asset"] == "HYPE"
    assert af["n_trades"] == 1 and af["n_windows"] == 1
    assert af["pnl_total_cents"] == pytest.approx(-70.0)          # HYPE drag
    assert af["book_excluding_asset_cents"] == pytest.approx(40.0)  # BTC alone (+40)


def test_net_of_fee_and_flat1_vs_staked(tmp_path):
    # One window doubled (stake=2) that LOSES, one flat winner. Verifies gross/net
    # (using the ledger's own total_cost_cents) and flat-1 vs staked, the axes that
    # made the headline move. cost=2c/contract on each.
    db = _build_v2(tmp_path, [
        _row("BTC-A", "10M", 100, side="NO", ask=60.0, stake=2, total_cost=2.0),  # loses: -60*2 gross
        _row("ETH-A", "10M", 101, side="NO", ask=60.0, stake=1, total_cost=2.0),  # wins: +40 gross
    ], resolutions={"BTC-A": "YES", "ETH-A": "NO"})
    ov = _audit(db, None, tmp_path, min_n=1)["account"]["overall"]
    # staked gross = -120 (BTC -60x2) + 40 = -80 ; fee = 2*2 + 2*1 = 6 ; net = -86
    assert ov["pnl_total_cents"] == pytest.approx(-80.0)
    assert ov["fee_total_cents"] == pytest.approx(6.0)
    assert ov["net_pnl_cents"] == pytest.approx(-86.0)
    # flat-1 gross = -60 (BTC unit) + 40 = -20 ; flat-1 fee = 2 + 2 = 4 ; net flat1 = -24
    assert ov["pnl_flat1_cents"] == pytest.approx(-20.0)
    assert ov["net_flat1_cents"] == pytest.approx(-24.0)


def test_stake_two_doubles_pnl(tmp_path):
    # stake=2: NO @ ask 60 settles NO -> +40 per unit * 2 = +80.
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0,
                                   stake=2)],
                   resolutions={"BTC-A": "NO"})
    rep = _audit(db, None, tmp_path, min_n=1)
    t = _trade(rep, "BTC-A", 100)
    assert t["stake"] == pytest.approx(2.0)
    assert t["recomputed_pnl_cents"] == pytest.approx(80.0)
    assert t["stored_pnl_cents"] == pytest.approx(80.0)
    # staked = ask * stake = 120.
    assert rep["account"]["overall"]["staked_cents"] == pytest.approx(120.0)


# --------------------------------------------------------------------------- #
# read-only guarantee
# --------------------------------------------------------------------------- #
def test_audit_is_read_only(tmp_path):
    db = _build_v2(tmp_path, [_row("BTC-A", "10M", 100, side="NO", ask=60.0)],
                   resolutions={"BTC-A": "NO"})
    before = sqlite3.connect(db).execute(
        "SELECT COUNT(*), SUM(hypothetical_pnl_cents) FROM ultoim_v2_predictions"
    ).fetchone()
    _audit(db, None, tmp_path, extend=False)
    _audit(db, None, tmp_path, min_n=1)
    after = sqlite3.connect(db).execute(
        "SELECT COUNT(*), SUM(hypothetical_pnl_cents) FROM ultoim_v2_predictions"
    ).fetchone()
    assert before == after  # nothing written to the live ledger
