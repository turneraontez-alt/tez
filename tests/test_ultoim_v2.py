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


def _with_flow(analysis, flow):
    """Attach the feature_values the cross-asset market-flow factor reads."""
    a = dict(analysis)
    a["feature_values"] = {"flow": flow, "momentum": 0.0}
    return a


# --------------------------------------------------------------------------- #
# skip_15m (default OFF): drop the weak 15M bin, keep 10M/7M untouched
# --------------------------------------------------------------------------- #
def test_skip_15m_default_off_fires_at_15m(tmp_path):
    # Default config (skip_15m False) still records/fires at the 15M mark.
    r = _runner(tmp_path, telegram=_StubTelegram())
    assert r.config.skip_15m is False
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    c = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
    rows = r.ledger.recent_rows("ultoim-v2", limit=10)
    assert [row["interval"] for row in rows] == ["15M"]


def test_skip_15m_suppresses_15m_but_keeps_10m(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram(), skip_15m=True)
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    # 15M band (900s): with skip_15m on, NOTHING is recorded (no fire, no abstain row).
    c15 = {"BTC": _canon("T-BTC", secs=900.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c15, now=1000.0), now=1000.0)
    assert r.scoreboard()["all_observations"]["recorded"] == 0
    # 10M band (600s), same contract/window: still records & fires normally.
    c10 = {"BTC": _canon("T-BTC", secs=600.0, close=9000.0)}
    r._observe_sync(candidates=_extract(r, a, c10, now=1300.0), now=1300.0)
    rows = r.ledger.recent_rows("ultoim-v2", limit=10)
    assert [row["interval"] for row in rows] == ["10M"]


# --------------------------------------------------------------------------- #
# record_xflow (default OFF): observe-only cross-asset market flow column
# --------------------------------------------------------------------------- #
def test_record_xflow_off_by_default_is_null(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram())
    assert r.config.record_xflow is False
    a = {"BTC": _with_flow(_analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                     mkt_yes=0.35), 0.4),
         "ETH": _with_flow(_analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                     mkt_yes=0.35), 0.2)}
    c = {"BTC": _canon("T-BTC", 900.0, 9000.0), "ETH": _canon("T-ETH", 900.0, 9000.0)}
    cands = _extract(r, a, c, now=1000.0)
    assert cands and all(cand.get("x_market_flow") is None for cand in cands)
    # And the recorded row's column stays NULL.
    r._observe_sync(candidates=cands, now=1000.0)
    rows = r.ledger.recent_rows("ultoim-v2", limit=10)
    assert rows and all(row["x_market_flow"] is None for row in rows)


def test_record_xflow_on_persists_market_mean(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram(), record_xflow=True)
    a = {"BTC": _with_flow(_analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                     mkt_yes=0.35), 0.4),
         "ETH": _with_flow(_analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                     mkt_yes=0.35), 0.2)}
    c = {"BTC": _canon("T-BTC", 900.0, 9000.0), "ETH": _canon("T-ETH", 900.0, 9000.0)}
    cands = _extract(r, a, c, now=1000.0)
    # market_flow = mean(0.4, 0.2) = 0.3, identical on every candidate this cycle.
    assert cands and all(cand["x_market_flow"] == pytest.approx(0.3) for cand in cands)
    # Persists through to the recorded row (one row per interval+window).
    r._observe_sync(candidates=cands, now=1000.0)
    rows = r.ledger.recent_rows("ultoim-v2", limit=10)
    assert rows and rows[0]["x_market_flow"] == pytest.approx(0.3)


def test_record_xflow_does_not_change_gate_decision(tmp_path):
    # The x_market_flow column is pure observation: turning it on must not change
    # which candidate fires (gate ignores it).
    a = {"BTC": _with_flow(_analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0,
                                     mkt_yes=0.35), 0.9)}
    c = {"BTC": _canon("T-BTC", 900.0, 9000.0)}
    fired = {}
    for flag in (False, True):
        r = _runner(tmp_path / f"x{int(flag)}", telegram=_StubTelegram(),
                    record_xflow=flag)
        r._observe_sync(candidates=_extract(r, a, c, now=1000.0), now=1000.0)
        rows = r.ledger.recent_rows("ultoim-v2", limit=10)
        fired[flag] = (rows[0]["fired"], rows[0]["predicted_side"], rows[0]["interval"])
    assert fired[False] == fired[True]
