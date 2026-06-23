"""Tests for the Ultoim V2 read-only PAPER entry-alert overlay.

Deterministic: no network, no live exchanges, no worker-thread timing (the sync
handlers _observe_sync / _reconcile_sync are driven directly). Verifies the pure
entry gate, the separate-records contract, the one-alert-per-contract-per-window
rule, the stale-feed abstain, settlement grading + scoreboard (Wilson CI + base
rate), the panel grammar (and the absence of live-formatter markers), and the
default-OFF guarantee.
"""
from __future__ import annotations

import types

import pytest

from q15_upgrade.ultoim_v2 import config as cfg_mod
from q15_upgrade.ultoim_v2 import gate, panel
from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger, _window_key
from q15_upgrade.ultoim_v2.runner import UltoimV2Runner, get_runner


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _StubTelegram:
    def __init__(self, delivered=True, message_id=777):
        self.delivered = delivered
        self.message_id = message_id
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        if self.delivered:
            return {"ok": True, "delivered": True, "muted": False,
                    "message_id": self.message_id, "error": None}
        return {"ok": False, "delivered": False, "muted": False,
                "message_id": None, "error": "boom"}


def _canon(ticker, secs, close, feed_ages=None):
    return types.SimpleNamespace(
        ticker=ticker, seconds_remaining=secs, settlement_time=close,
        feed_ages=feed_ages or {})


def _analysis(side="NO", sel=0.60, ask=60.0, net_edge=3.0, dq=0.8, eq=0.8,
              yes=0.40, mkt_yes=0.40, total_cost=2.0, manip=False, stale=None):
    a = {
        "prediction_available": True,
        "prediction_side": side,
        "selected_probability": sel,
        "conservative_probability": sel - 0.02,
        "yes_probability": yes,
        "raw_yes_probability": yes,
        "market_implied_yes_probability": mkt_yes,
        "data_quality": dq,
        "evidence_quality": eq,
        "net_edge_cents": net_edge,
        "entry_ask_cents": ask,
        "quote": {"ask_cents": ask, "spread_cents": 2.0, "depth_contracts": 50.0,
                  "quote_age_seconds": 1.0},
        "costs": {"fee_cents": 1.0, "total_cost_cents": total_cost},
        "regime": {"name": "TREND", "distance_sigma": 1.2},
        "manipulation": {"suspected": manip},
        "flip_risk": {"score": 20.0},
        "shadow_signals": {"order_flow_persistence": 0.05, "book_resiliency": 0.05,
                           "prediction_stability": 0.05},
        "snapshot_id": "snap-1",
    }
    if stale is not None:
        a["spot_stale_age_seconds"] = stale
    return a


def _config(tmp_path, **over):
    return UltoimV2Config(
        enabled=True, model_version="ultoim-v2",
        db_path=str(tmp_path / "ultoim_v2.sqlite3"),
        telegram_chat_id="", min_confidence=0.55, ask_lo=50.0, ask_hi=72.0,
        min_edge_cents=2.0, no_only=True, mark_band_seconds=90.0,
        reconcile_every_seconds=0.0, recap_every_seconds=0.0,
        max_spot_stale_seconds=8.0, min_scoreboard_n=30, **over,
    )


def _runner(tmp_path, telegram=None, **over):
    r = UltoimV2Runner(_config(tmp_path, **over))
    if telegram is not None:
        r.telegram = telegram
    return r


def _candidate(**over):
    cand = {
        "predicted_side": "NO", "selected_probability": 0.60, "entry_ask_cents": 60.0,
        "total_cost_cents": 0.0,
    }
    cand.update(over)
    return cand


# --------------------------------------------------------------------------- #
# gate.evaluate
# --------------------------------------------------------------------------- #
def test_gate_fires_clean_no():
    cfg = UltoimV2Config(enabled=True)
    # Clean NO at conf .65 / ask 60 / cost 0 -> net edge 0.65*100-60 = 5 >= 2 -> fires.
    v = gate.evaluate(_candidate(predicted_side="NO", selected_probability=0.65,
                                 entry_ask_cents=60.0, total_cost_cents=0.0), cfg)
    assert v["fired"] is True
    assert v["gate_a"] and v["gate_b"] and v["gate_c"]
    assert v["reason_codes"] == []
    assert v["net_edge_cents"] == pytest.approx(5.0)


def test_gate_blocks_yes_side():
    cfg = UltoimV2Config(enabled=True)
    v = gate.evaluate(_candidate(predicted_side="YES", selected_probability=0.65), cfg)
    assert v["fired"] is False
    assert "WRONG_SIDE_YES" in v["reason_codes"]


def test_gate_blocks_low_confidence():
    cfg = UltoimV2Config(enabled=True)
    v = gate.evaluate(_candidate(selected_probability=0.50, entry_ask_cents=48.0), cfg)
    assert v["fired"] is False
    assert "CONF_BELOW_MIN" in v["reason_codes"]


def test_gate_blocks_ask_below_band():
    cfg = UltoimV2Config(enabled=True)
    v = gate.evaluate(_candidate(selected_probability=0.60, entry_ask_cents=45.0), cfg)
    assert "ASK_BELOW_BAND" in v["reason_codes"] and v["fired"] is False


def test_gate_blocks_ask_above_band():
    cfg = UltoimV2Config(enabled=True)
    v = gate.evaluate(_candidate(selected_probability=0.85, entry_ask_cents=80.0), cfg)
    assert "ASK_ABOVE_BAND" in v["reason_codes"] and v["fired"] is False


def test_gate_blocks_edge_below_min():
    cfg = UltoimV2Config(enabled=True)
    # edge = 60 - 59 - 0 = 1.0 < 2.0
    v = gate.evaluate(_candidate(selected_probability=0.60, entry_ask_cents=59.0,
                                 total_cost_cents=0.0), cfg)
    assert "EDGE_BELOW_MIN" in v["reason_codes"] and v["fired"] is False


def test_gate_missing_data():
    cfg = UltoimV2Config(enabled=True)
    assert gate.evaluate(_candidate(entry_ask_cents=None), cfg)["reason_codes"] == ["MISSING_DATA"]
    assert gate.evaluate(_candidate(selected_probability=None), cfg)["reason_codes"] == ["MISSING_DATA"]
    none_v = gate.evaluate(_candidate(entry_ask_cents=None), cfg)
    assert none_v["fired"] is False and none_v["net_edge_cents"] is None


def test_gate_inclusive_bounds():
    cfg = UltoimV2Config(enabled=True)
    # ask exactly 50 (lo) passes the band; conf high enough; edge ok
    lo = gate.evaluate(_candidate(selected_probability=0.60, entry_ask_cents=50.0,
                                  total_cost_cents=0.0), cfg)
    assert lo["gate_b"] and lo["fired"]
    # ask exactly 72 (hi) passes the band
    hi = gate.evaluate(_candidate(selected_probability=0.80, entry_ask_cents=72.0,
                                  total_cost_cents=0.0), cfg)
    assert hi["gate_b"] and hi["fired"]
    # conf exactly at min passes
    conf = gate.evaluate(_candidate(selected_probability=0.55, entry_ask_cents=50.0,
                                    total_cost_cents=0.0), cfg)
    assert conf["gate_b"]
    # edge exactly 2.0 passes (62 - 60 - 0 = 2.0)
    edge = gate.evaluate(_candidate(selected_probability=0.62, entry_ask_cents=60.0,
                                    total_cost_cents=0.0), cfg)
    assert edge["net_edge_cents"] == pytest.approx(2.0) and edge["fired"]


# --------------------------------------------------------------------------- #
# best_entry_cents / display_entry
# --------------------------------------------------------------------------- #
def test_best_entry_cents_floor_and_clamp():
    cfg = UltoimV2Config(enabled=True)  # ask_lo 50, ask_hi 72, min_edge 2
    # 0.655*100 - 1.0 - 2.0 = 62.5 -> floor 62, within band
    assert gate.best_entry_cents(0.655, 1.0, cfg) == 62
    # very high -> clamped to ask_hi (72)
    assert gate.best_entry_cents(0.99, 0.0, cfg) == 72
    # very low -> clamped to ask_lo (50)
    assert gate.best_entry_cents(0.40, 0.0, cfg) == 50


def test_display_entry_never_above_ask():
    cfg = UltoimV2Config(enabled=True)
    # best=72 (clamped) but ask is 60 -> display is 60
    assert gate.display_entry(0.99, 0.0, 60.0, cfg) == 60
    # best below ask -> shows best
    assert gate.display_entry(0.655, 1.0, 70.0, cfg) == 62


# --------------------------------------------------------------------------- #
# ledger: record / resolve / alert-lock / scoreboard / loss_rows
# --------------------------------------------------------------------------- #
def _row(**over):
    row = {
        "created_at": 1000.0, "model_version": "ultoim-v2", "asset": "BTC",
        "ticker": "T-BTC", "interval": "10M", "window_key": 5, "mark_seconds": 600.0,
        "fired": 1, "predicted_side": "NO", "selected_probability": 0.60,
        "calibrated_yes_probability": 0.40, "conservative_probability": 0.58,
        "market_implied_yes_probability": 0.40, "raw_yes_probability": 0.40,
        "net_edge_cents": 3.0, "entry_ask_cents": 60.0, "best_entry_cents": 58,
        "fee_cents": 1.0, "total_cost_cents": 2.0, "spread_cents": 2.0,
        "depth_contracts": 50.0, "quote_age_seconds": 1.0, "spot_stale_age_seconds": None,
        "distance_sigma": 1.2, "regime_name": "TREND", "regime_directional": "NO_PRONE",
        "data_quality": 0.8, "evidence_quality": 0.8, "manipulation_suspected": 0,
        "flip_probability": 20.0, "order_flow_persistence": 0.05, "book_resiliency": 0.05,
        "prediction_stability": 0.05, "gate_a_pass": 1, "gate_b_pass": 1, "gate_c_pass": 1,
        "reason_codes": None, "gate_min_conf": 0.55, "gate_ask_lo": 50.0,
        "gate_ask_hi": 72.0, "gate_min_edge": 2.0, "close_time": 9000.0,
        "snapshot_id": "snap-1", "session_id": "1000.0", "delivery_status": "SENT",
    }
    row.update(over)
    return row


def test_record_decision_idempotent(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    assert led.record_decision(_row()) is not None
    assert led.record_decision(_row()) is None              # dup (version, ticker, interval)
    assert led.record_decision(_row(interval="7M")) is not None
    led.close()


def test_resolve_grades_and_pnl_idempotent(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_row(ticker="T1", predicted_side="NO", entry_ask_cents=60.0))  # NO->NO win
    led.record_decision(_row(ticker="T2", interval="7M", predicted_side="NO",
                             entry_ask_cents=60.0))                                     # NO settles YES -> loss
    assert led.resolve("ultoim-v2", "T1", "NO", 9500.0) == 1
    assert led.resolve("ultoim-v2", "T2", "YES", 9500.0) == 1
    assert led.resolve("ultoim-v2", "T1", "NO", 9600.0) == 0   # idempotent re-grade

    sb = led.scoreboard("ultoim-v2", min_n=1)
    assert sb["resolved"] == 2
    assert sb["overall"]["right"] == 1 and sb["overall"]["wrong"] == 1
    # T1 win +40 (100-60); T2 loss -60 -> total -20
    assert sb["overall"]["pnl_total_cents"] == pytest.approx(-20.0)
    led.close()


def test_alert_lock_one_per_contract_per_window(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    assert led.claim_alert("ultoim-v2", "T-BTC", 5, 1.0) is True
    assert led.claim_alert("ultoim-v2", "T-BTC", 5, 2.0) is False   # same contract+window
    assert led.alert_locked("ultoim-v2", "T-BTC", 5) is True
    assert led.claim_alert("ultoim-v2", "T-BTC", 6, 3.0) is True    # next window ok
    led.close()


def test_scoreboard_wilson_baserate_and_edge(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # 3 NO picks: 2 settle NO (win), 1 settles YES (loss). base rate side = NO (2/3).
    for i, res in enumerate(("NO", "NO", "YES")):
        led.record_decision(_row(ticker=f"T{i}", interval="10M" if i == 0 else "7M" if i == 1 else "15M",
                                 predicted_side="NO", entry_ask_cents=60.0))
        led.resolve("ultoim-v2", f"T{i}", res, 9500.0)
    sb = led.scoreboard("ultoim-v2", min_n=1)
    assert sb["resolved"] == 3
    ov = sb["overall"]
    assert ov["right"] == 2 and ov["wrong"] == 1
    assert ov["accuracy"] == pytest.approx(2 / 3)
    assert ov["ci_low"] is not None and ov["ci_high"] is not None
    assert ov["base_rate"] == pytest.approx(2 / 3) and ov["base_rate_side"] == "NO"
    # accuracy 2/3 == base rate 2/3 -> edge ~0
    assert ov["edge_over_base"] == pytest.approx(0.0)
    led.close()


def test_loss_rows_returns_wrong_entries(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_row(ticker="W1", predicted_side="NO", entry_ask_cents=60.0))
    led.record_decision(_row(ticker="W2", interval="7M", predicted_side="NO", entry_ask_cents=55.0))
    led.resolve("ultoim-v2", "W1", "NO", 9500.0)    # win
    led.resolve("ultoim-v2", "W2", "YES", 9500.0)   # loss
    losses = led.loss_rows("ultoim-v2", limit=10)
    assert len(losses) == 1 and losses[0]["ticker"] == "W2"
    assert losses[0]["correct"] == 0
    led.close()


# --------------------------------------------------------------------------- #
# config default-OFF
# --------------------------------------------------------------------------- #
def test_default_off(monkeypatch):
    monkeypatch.delenv("Q15_ULTOIM_V2_ENABLED", raising=False)
    cfg_mod.reset_enabled_cache()
    assert UltoimV2Config.from_env().enabled is False
    assert cfg_mod.is_enabled() is False
    assert get_runner() is None
    cfg_mod.reset_enabled_cache()


def test_explicit_enable(monkeypatch, tmp_path):
    monkeypatch.setenv("Q15_ULTOIM_V2_ENABLED", "true")
    monkeypatch.setenv("Q15_ULTOIM_V2_DB", str(tmp_path / "enabled.sqlite3"))
    cfg_mod.reset_enabled_cache()
    assert UltoimV2Config.from_env().enabled is True
    from q15_upgrade.ultoim_v2.runner import reset_runner_for_tests
    reset_runner_for_tests()
    r = get_runner()
    assert r is not None
    reset_runner_for_tests()
    cfg_mod.reset_enabled_cache()


# --------------------------------------------------------------------------- #
# panel
# --------------------------------------------------------------------------- #
_FORBIDDEN = ("V9.5 CHECK", "ENTRY RECOMMENDED", "Hourly Report —", "TOP 3 PICKS")


def test_build_entry_alert_grammar_and_no_collision():
    cfg = UltoimV2Config(enabled=True)
    pick = {
        "asset": "BTC", "predicted_side": "NO", "ticker": "T-BTC", "interval": "10M",
        "window_key": 5, "selected_probability": 0.62, "entry_ask_cents": 60.0,
        "best_entry_cents": 58, "net_edge_cents": 2.5,
    }
    text = panel.build_entry_alert(pick, {"resolved": 0}, cfg)
    assert "ULTOIM V2" in text and "PAPER" in text
    assert "T-BTC" in text and "BTC NO" in text and "58¢ or lower" in text
    assert "RESEARCH SIGNAL" in text
    for marker in _FORBIDDEN:
        assert marker not in text


def test_build_recap_insufficient_data():
    cfg = UltoimV2Config(enabled=True, min_scoreboard_n=30)
    sb = {
        "min_n": 30, "resolved": 3, "total_recorded": 5,
        "delivery_counts": {"SENT": 3, "RECORDED": 2},
        "overall": {"n": 3, "right": 2, "wrong": 1, "accuracy": 2 / 3,
                    "ci_low": 0.2, "ci_high": 0.9, "low_n": True,
                    "pnl_total_cents": -20.0, "pnl_avg_cents": -6.67,
                    "base_rate": 2 / 3, "base_rate_side": "NO", "edge_over_base": 0.0},
        "by_interval": {iv: {"n": 0, "right": 0, "wrong": 0} for iv in ("15M", "10M", "7M")},
    }
    text = panel.build_recap(sb, [], [], cfg)
    assert "RESEARCH RECAP" in text
    assert "INSUFFICIENT DATA (N<30)" in text
    for marker in _FORBIDDEN:
        assert marker not in text


# --------------------------------------------------------------------------- #
# runner end-to-end (sync handlers, stub telegram, fake resolver)
# --------------------------------------------------------------------------- #
def test_runner_fires_one_alert_then_dedup_and_stale(tmp_path):
    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg)
    wk = _window_key(9000.0, 1000.0)

    # 15M mark (900s): a clean NO that fires.
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)

    sb = r.scoreboard()
    assert sb["all_observations"]["fired"] == 1
    assert len(tg.sent) == 1 and "ULTOIM V2" in tg.sent[0]
    assert r.ledger.alert_locked("ultoim-v2", "T-BTC", wk)

    # 10M mark (600s), SAME contract + window: recorded but NOT alerted again.
    c2 = {"BTC": _canon("T-BTC", secs=600.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c2, now=1300.0), now=1300.0)
    assert len(tg.sent) == 1                                # no second alert
    assert r.scoreboard()["all_observations"]["recorded"] == 2

    # 7M mark (420s), a DIFFERENT contract on a stale feed: abstain w/ STALE_FEED.
    a_stale = {"ETH": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                mkt_yes=0.35, stale=30.0)}
    c_stale = {"ETH": _canon("T-ETH", secs=420.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a_stale, c_stale, now=1580.0), now=1580.0)
    assert len(tg.sent) == 1                                # stale -> no alert
    losses = r.ledger.recent_rows("ultoim-v2", limit=10)
    eth_rows = [row for row in losses if row["ticker"] == "T-ETH"]
    assert eth_rows and eth_rows[0]["fired"] == 0
    assert "STALE_FEED" in (eth_rows[0]["reason_codes"] or "")


def test_runner_records_abstain_when_nothing_fires(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram())
    # YES side with no_only=True -> abstains; still records the best would-be row.
    a = {"BTC": _analysis(side="YES", sel=0.65, ask=60.0, mkt_yes=0.70)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
    sb = r.scoreboard()
    assert sb["all_observations"]["recorded"] == 1
    assert sb["all_observations"]["fired"] == 0


def test_runner_reconcile_grades_against_resolver(tmp_path):
    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg)
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)

    class _Resolver:
        def get_market(self, ticker):
            return {"result": "NO"}

    r._reconcile_sync(resolver=_Resolver(), now=9500.0)
    sb = r.scoreboard()
    assert sb["resolved"] == 1
    assert sb["overall"]["right"] == 1


def test_runner_observe_only_in_band(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram())
    a = {"BTC": _analysis()}
    # 300s left: not in any band ([810,900],[510,600],[330,420])
    c = {"BTC": _canon("T-BTC", secs=300.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
    assert r.scoreboard()["all_observations"]["recorded"] == 0


def _extract(runner, analyses, canonicals, now):
    """Run the synchronous extraction observe() does, capturing the candidates."""
    captured = {}
    orig = runner._jobs.put_nowait
    runner._jobs.put_nowait = lambda item: captured.update(item[1])  # type: ignore
    try:
        runner._ensure_worker = lambda: None  # type: ignore
        runner.observe(analyses=analyses, canonicals=canonicals, now=now)
    finally:
        runner._jobs.put_nowait = orig  # type: ignore
    return captured.get("candidates", [])


# --------------------------------------------------------------------------- #
# gate: correctness fixes (float boundary, display clamp, bool reject, research)
# --------------------------------------------------------------------------- #
def test_gate_edge_float_boundary_admits_true_two_cent_edge():
    """0.58*100 - 56 == 1.999999999999993 in binary float; the inclusive 2.0¢ bar
    must still admit a mathematically-exact 2.0¢ edge (the one fitted knob)."""
    cfg = UltoimV2Config(enabled=True)
    assert 0.58 * 100.0 - 56.0 < 2.0  # the float underflow is real
    v = gate.evaluate(_candidate(predicted_side="NO", selected_probability=0.58,
                                 entry_ask_cents=56.0, total_cost_cents=0.0), cfg)
    assert v["gate_c"] is True and "EDGE_BELOW_MIN" not in v["reason_codes"]
    assert v["fired"] is True
    # a genuine sub-2¢ edge still fails (no over-admitting).
    v2 = gate.evaluate(_candidate(predicted_side="NO", selected_probability=0.575,
                                  entry_ask_cents=56.0, total_cost_cents=0.0), cfg)
    assert v2["gate_c"] is False and "EDGE_BELOW_MIN" in v2["reason_codes"]


def test_gate_rejects_bool_as_missing_data():
    cfg = UltoimV2Config(enabled=True)
    for bad in ({"predicted_side": "NO", "selected_probability": True, "entry_ask_cents": 60.0},
                {"predicted_side": "NO", "selected_probability": 0.6, "entry_ask_cents": False}):
        v = gate.evaluate(bad, cfg)
        assert v["reason_codes"] == ["MISSING_DATA"]
        assert v["fired"] is False and v["net_edge_cents"] is None


def test_display_entry_floors_and_reclamps_into_band():
    cfg = UltoimV2Config(enabled=True)
    # int(49.9)==49 would advertise a price BELOW the ask_lo=50 band floor.
    assert gate.display_entry(0.99, 0.0, 49.9, cfg) == 50
    # normal: best-entry below ask shows the best-entry.
    assert gate.display_entry(0.655, 1.0, 70.0, cfg) == 62


def test_gate_research_fired_yes_side_never_delivers():
    cfg = UltoimV2Config(enabled=True)  # no_only=True
    v = gate.evaluate(_candidate(predicted_side="YES", selected_probability=0.65,
                                 entry_ask_cents=60.0, total_cost_cents=2.0), cfg)
    assert v["fired"] is False                       # NO-only delivery
    assert "WRONG_SIDE_YES" in v["reason_codes"]
    assert v["research_fired"] is True               # but it cleared conf+ask+edge


def test_gate_collects_every_failing_reason():
    cfg = UltoimV2Config(enabled=True)
    v = gate.evaluate(_candidate(predicted_side="YES", selected_probability=0.40,
                                 entry_ask_cents=45.0, total_cost_cents=0.0), cfg)
    assert set(v["reason_codes"]) == {
        "WRONG_SIDE_YES", "CONF_BELOW_MIN", "ASK_BELOW_BAND", "EDGE_BELOW_MIN"}


# --------------------------------------------------------------------------- #
# validate: significance math + verdict
# --------------------------------------------------------------------------- #
from q15_upgrade.ultoim_v2 import validate  # noqa: E402


def _vrow(side="NO", result="NO", interval="10M", net_edge=3.0, window_key=1,
          regime="NO_PRONE", research_fired=1, pnl=None):
    correct = 1 if side.upper() == result.upper() else 0
    if pnl is None:
        pnl = 40.0 if correct else -60.0
    return {"predicted_side": side, "official_result": result, "interval": interval,
            "net_edge_cents": net_edge, "window_key": window_key, "correct": correct,
            "regime_directional": regime, "research_fired": research_fired,
            "fired": 1 if side.upper() == "NO" else 0, "hypothetical_pnl_cents": pnl}


def test_wilson_known_values():
    assert validate.wilson(0, 0) == (None, None, None)
    p, lo, hi = validate.wilson(2, 3)
    assert (p, round(lo, 4), round(hi, 4)) == (pytest.approx(2 / 3), 0.2077, 0.9385)
    _, lo, hi = validate.wilson(8, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.4902, 0.9433)
    _, lo, _ = validate.wilson(0, 10)
    assert lo >= 0.0                                  # never negative
    _, _, hi = validate.wilson(10, 10)
    assert hi == pytest.approx(1.0)


def test_binom_test_greater_known_values():
    assert validate.binom_test_greater(11, 12, 0.75) == pytest.approx(0.1584, abs=1e-3)
    assert validate.binom_test_greater(5, 6, 0.75) == pytest.approx(0.5339, abs=1e-3)
    assert validate.binom_test_greater(21, 23, 0.75) == pytest.approx(0.0492, abs=1e-3)
    assert validate.binom_test_greater(0, 10, 0.5) == 1.0     # k<=0
    assert validate.binom_test_greater(5, 0, 0.5) == 1.0      # n<=0


def test_lift_vs_base_separability():
    # 11/12 NO wins is NOT separable from base rate (p=0.158).
    rows = [_vrow(result="NO") for _ in range(11)] + [_vrow(result="YES")]
    lift = validate.lift_vs_base(rows)
    assert lift["n"] == 12 and lift["hits"] == 11
    assert lift["base_rate"] == pytest.approx(11 / 12)
    assert lift["separable"] is False
    # 700/1000 vs a 50/50 base IS separable.
    big = ([_vrow(result="NO") for _ in range(700)]
           + [_vrow(result="YES") for _ in range(300)])
    lift2 = validate.lift_vs_base(big)
    assert lift2["base_rate"] == pytest.approx(0.7)
    # accuracy == base here (all NO bets, 70% settle NO) -> NOT separable
    assert lift2["separable"] is False


def test_edge_bucket_monotonicity():
    # higher edge -> higher accuracy (monotone)
    mono = ([_vrow(net_edge=2.5, result="YES") for _ in range(4)]    # low edge, 0% in [2,3)
            + [_vrow(net_edge=2.5, result="NO")]
            + [_vrow(net_edge=10.0, result="NO") for _ in range(5)])  # high edge, 100%
    out = validate.edge_bucket_monotonicity(mono)
    assert out["is_monotone"] is True and out["n_populated"] == 2
    # inverted -> not monotone
    inv = ([_vrow(net_edge=2.5, result="NO") for _ in range(5)]
           + [_vrow(net_edge=10.0, result="YES") for _ in range(5)])
    assert validate.edge_bucket_monotonicity(inv)["is_monotone"] is False


def test_realized_window_regime_classification():
    rows = ([_vrow(result="YES", window_key=1) for _ in range(3)]    # window 1: all YES
            + [_vrow(result="NO", window_key=1)]                     # -> 3/4 YES = YES_PRONE
            + [_vrow(result="NO", window_key=2) for _ in range(4)])  # window 2: all NO
    regime = validate.realized_window_regime(rows)
    assert regime[1] == "YES_PRONE" and regime[2] == "NO_PRONE"


def test_verdict_from_agg_states():
    cfg_n = 50
    insufficient = {"n": 11, "right": 10, "ci_low": 0.6, "base_rate": 0.5}
    assert validate.verdict_from_agg(insufficient, min_promote_n=cfg_n)["state"] == validate.INSUFFICIENT
    not_sep = {"n": 60, "right": 33, "ci_low": 0.45, "base_rate": 0.5}
    assert validate.verdict_from_agg(not_sep, min_promote_n=cfg_n)["state"] == validate.NOT_SEPARABLE
    beats = {"n": 1000, "right": 700, "ci_low": 0.671, "base_rate": 0.5}
    assert validate.verdict_from_agg(beats, min_promote_n=cfg_n)["state"] == validate.BEATS


def test_validation_report_requires_yes_side_and_regime():
    # Only NO-prone NO rows that beat a low base -> overall may beat, but never
    # 'fully_proven' while YES side / YES-prone regime are empty.
    rows = ([_vrow(result="NO", regime="NO_PRONE", window_key=i) for i in range(60)]
            + [_vrow(result="YES", regime="NO_PRONE", window_key=i) for i in range(20)])
    rep = validate.validation_report(rows, min_promote_n=50)
    assert rep["available"] is True
    assert rep["by_side"]["YES"]["n"] == 0           # no YES research rows here
    assert rep["fully_proven"] is False


# --------------------------------------------------------------------------- #
# ledger: by_side split, research rows, resolved_rows, migration/restart
# --------------------------------------------------------------------------- #
def test_scoreboard_by_side_splits_no_and_yes_research(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # delivered NO entry (win) and a YES research row (loss) — different contracts.
    led.record_decision(_row(ticker="N1", predicted_side="NO", fired=1,
                             research_fired=1, record_kind="DELIVERED_CANDIDATE"))
    led.record_decision(_row(ticker="Y1", interval="7M", predicted_side="YES", fired=0,
                             research_fired=1, record_kind="RESEARCH_YES"))
    led.resolve("ultoim-v2", "N1", "NO", 9500.0)     # NO win
    led.resolve("ultoim-v2", "Y1", "NO", 9500.0)     # YES predicted, settles NO -> loss
    sb = led.scoreboard("ultoim-v2", min_n=1)
    assert sb["by_side"]["NO"]["n"] == 1 and sb["by_side"]["NO"]["right"] == 1
    assert sb["by_side"]["YES"]["n"] == 1 and sb["by_side"]["YES"]["right"] == 0
    # the YES research row never inflates the delivered headline (fired==1 only).
    assert sb["overall"]["n"] == 1 and sb["resolved"] == 1
    assert sb["research_resolved"] == 2
    led.close()


def test_resolved_rows_oldest_first(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_row(ticker="A", created_at=2000.0))
    led.record_decision(_row(ticker="B", interval="7M", created_at=1000.0))
    led.resolve("ultoim-v2", "A", "NO", 9500.0)
    led.resolve("ultoim-v2", "B", "NO", 9500.0)
    rows = led.resolved_rows("ultoim-v2")
    assert [r["ticker"] for r in rows] == ["B", "A"]      # oldest created_at first
    led.close()


def test_report_lock_idempotent(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    assert led.lock_report("ultoim-v2", "10M", 5, 1.0) is True
    assert led.lock_report("ultoim-v2", "10M", 5, 2.0) is False
    assert led.report_locked("ultoim-v2", "10M", 5) is True
    assert led.lock_report("ultoim-v2", "10M", 6, 3.0) is True     # next window
    led.close()


def test_base_rate_ties_break_no(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_row(ticker="T1", predicted_side="NO"))
    led.record_decision(_row(ticker="T2", interval="7M", predicted_side="NO"))
    led.resolve("ultoim-v2", "T1", "NO", 9500.0)
    led.resolve("ultoim-v2", "T2", "YES", 9500.0)
    sb = led.scoreboard("ultoim-v2", min_n=1)
    assert sb["overall"]["base_rate"] == pytest.approx(0.5)
    assert sb["overall"]["base_rate_side"] == "NO"        # tie -> NO
    led.close()


def test_restart_preserves_marker_and_rows(tmp_path):
    path = str(tmp_path / "u.sqlite3")
    led = UltoimV2Ledger(path)
    led.ensure_reset_marker("ultoim-v2", 1234.0)
    reset_at, sess = led.reset_at(), led.session_id()
    led.record_decision(_row(ticker="P1"))
    led.close()
    led2 = UltoimV2Ledger(path)                           # reopen (simulated restart)
    assert led2.reset_at() == reset_at and led2.session_id() == sess
    assert led2.record_decision(_row(ticker="P1")) is None   # dup still rejected
    led2.close()


# --------------------------------------------------------------------------- #
# runner: YES-side research recording alongside the delivered NO
# --------------------------------------------------------------------------- #
def test_runner_records_research_yes_alongside_delivered_no(tmp_path):
    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg)
    wk = _window_key(9000.0, 1000.0)
    a = {
        "BTC": _analysis(side="NO", sel=0.65, ask=60.0, mkt_yes=0.35),   # fires + delivered
        "ETH": _analysis(side="YES", sel=0.65, ask=60.0, mkt_yes=0.70),  # research-only
    }
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0),
         "ETH": _canon("T-ETH", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)

    assert len(tg.sent) == 1 and "BTC NO" in tg.sent[0]   # only the NO is delivered
    rows = {row["ticker"]: row for row in r.ledger.recent_rows("ultoim-v2", limit=10)}
    assert rows["T-BTC"]["record_kind"] == "DELIVERED_CANDIDATE" and rows["T-BTC"]["fired"] == 1
    eth = rows["T-ETH"]
    assert eth["record_kind"] == "RESEARCH_YES" and eth["fired"] == 0
    assert eth["delivery_status"] == "RESEARCH" and eth["research_fired"] == 1
    assert not r.ledger.alert_locked("ultoim-v2", "T-ETH", wk)   # YES never claims the alert lock


def test_runner_research_yes_can_be_disabled(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram(), record_research_yes=False)
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, mkt_yes=0.35),
         "ETH": _analysis(side="YES", sel=0.65, ask=60.0, mkt_yes=0.70)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0),
         "ETH": _canon("T-ETH", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
    tickers = {row["ticker"] for row in r.ledger.recent_rows("ultoim-v2", limit=10)}
    assert tickers == {"T-BTC"}                            # no YES research row written


def test_runner_stale_boundary_inclusive(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram())   # _config pins max_spot_stale=8.0
    # exactly at the limit (8.0) still fires; just over (8.01) abstains STALE_FEED.
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, mkt_yes=0.35, stale=8.0)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
    assert r.scoreboard()["all_observations"]["fired"] == 1

    r2 = _runner(tmp_path / "b", telegram=_StubTelegram())
    a2 = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, mkt_yes=0.35, stale=8.01)}
    r2._observe_sync(candidates=_extract(r2, a2, c, now=1000.0), now=1000.0)
    row = r2.ledger.recent_rows("ultoim-v2", limit=5)[0]
    assert row["fired"] == 0 and "STALE_FEED" in (row["reason_codes"] or "")


# --------------------------------------------------------------------------- #
# panel: promotion verdict + side/regime split + forbidden-marker completeness
# --------------------------------------------------------------------------- #
# Full set of live suppression/routing markers V2 must never emit (the test tuple
# above omitted NO ENTRY YET; the recap upgrade adds 'NO-prone' wording, so guard it).
_FORBIDDEN_FULL = ("V9.5 CHECK", "ENTRY RECOMMENDED", "NO ENTRY YET",
                   "Hourly Report —", "TOP 3 PICKS")


def _recap_sb(*, n, right, ci_low, base_rate, min_n=30, yes_n=0, yes_prone_n=0):
    agg0 = {"n": 0, "right": 0, "wrong": 0}
    overall = {"n": n, "right": right, "wrong": n - right,
               "accuracy": (right / n) if n else None, "ci_low": ci_low, "ci_high": 1.0,
               "low_n": n < min_n, "pnl_total_cents": 0.0, "pnl_avg_cents": 0.0,
               "base_rate": base_rate, "base_rate_side": "NO", "edge_over_base": 0.1}
    return {
        "min_n": min_n, "resolved": n, "total_recorded": n, "delivery_counts": {},
        "overall": overall,
        "by_interval": {iv: dict(agg0) for iv in ("15M", "10M", "7M")},
        "by_side": {"NO": dict(agg0, n=n), "YES": dict(agg0, n=yes_n)},
        "by_regime_directional": {"NO_PRONE": dict(agg0, n=n),
                                  "YES_PRONE": dict(agg0, n=yes_prone_n),
                                  "BALANCED": dict(agg0)},
    }


def test_recap_promotion_insufficient():
    cfg = UltoimV2Config(enabled=True, min_promote_n=50)
    text = panel.build_recap(_recap_sb(n=11, right=10, ci_low=0.6, base_rate=0.5), [], [], cfg)
    assert "PROMOTION: INSUFFICIENT DATA (N<50)" in text
    for m in _FORBIDDEN_FULL:
        assert m not in text


def test_recap_promotion_not_separable():
    cfg = UltoimV2Config(enabled=True, min_promote_n=50)
    text = panel.build_recap(_recap_sb(n=60, right=33, ci_low=0.45, base_rate=0.5), [], [], cfg)
    assert "PROMOTION: NOT SEPARABLE FROM BASE RATE" in text
    assert "BEATS BASE RATE" not in text
    for m in _FORBIDDEN_FULL:
        assert m not in text


def test_recap_promotion_beats_with_unproven_qualifier():
    cfg = UltoimV2Config(enabled=True, min_promote_n=50)
    # overall beats, but YES side / YES-prone still empty -> qualifier appended.
    text = panel.build_recap(
        _recap_sb(n=1000, right=700, ci_low=0.671, base_rate=0.5, yes_n=0, yes_prone_n=0),
        [], [], cfg)
    assert "PROMOTION: BEATS BASE RATE (sig-tested" in text
    assert "YES-side / YES-prone unproven" in text
    for m in _FORBIDDEN_FULL:
        assert m not in text


def test_recap_shows_side_and_regime_sections():
    cfg = UltoimV2Config(enabled=True, min_promote_n=50)
    text = panel.build_recap(_recap_sb(n=40, right=34, ci_low=0.7, base_rate=0.5,
                                       yes_n=5, yes_prone_n=3), [], [], cfg)
    assert "By side:" in text and "(research-only)" in text
    assert "By regime:" in text and "YES-prone:" in text


def test_entry_card_caveat_is_derived_not_hardcoded():
    cfg = UltoimV2Config(enabled=True)
    pick = {"asset": "BTC", "predicted_side": "NO", "ticker": "T-BTC", "interval": "10M",
            "window_key": 5, "selected_probability": 0.62, "entry_ask_cents": 60.0,
            "best_entry_cents": 58, "net_edge_cents": 2.5}
    # a summary reflecting real YES-prone data must NOT print "0 YES-prone".
    text = panel.build_entry_alert(pick, {"resolved": 40, "n_regimes": 2, "yes_prone_n": 7}, cfg)
    assert "7 YES-prone" in text and "0 YES-prone" not in text
    assert "2 regimes" in text
    for m in _FORBIDDEN_FULL:
        assert m not in text
