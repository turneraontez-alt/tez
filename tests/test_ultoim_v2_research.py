"""Tests for the Ultoim V2 review-consensus additions (all record-only / robustness):

  * ledger.distance_research_scoreboard — the near-strike-pin vs far split (the one
    record-only feature shown to transport), measured prospectively.
  * panel.build_recap surfaces the s15 + distance research screens, marker-safe.
  * runner.observe() isolates a malformed asset per-candidate (never raises / never
    drops the whole cycle).
  * the REAL worker loop (queue + thread + task_done) records an observe job.

Deterministic: stub Telegram, no network. The worker-loop test uses the real thread
but joins the queue, so it is still deterministic.
"""
from __future__ import annotations

import types

import pytest

from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger
from q15_upgrade.ultoim_v2 import panel
from q15_upgrade.ultoim_v2.runner import UltoimV2Runner


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _StubTelegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)
        return {"ok": True, "delivered": True, "muted": False, "message_id": 1, "error": None}


def _canon(ticker, secs, close):
    return types.SimpleNamespace(ticker=ticker, seconds_remaining=secs,
                                 settlement_time=close, feed_ages={})


def _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35, **extra):
    a = {
        "prediction_available": True, "prediction_side": side,
        "selected_probability": sel, "conservative_probability": sel - 0.02,
        "yes_probability": mkt_yes, "raw_yes_probability": mkt_yes,
        "market_implied_yes_probability": mkt_yes, "data_quality": 0.8,
        "evidence_quality": 0.8, "net_edge_cents": net_edge, "entry_ask_cents": ask,
        "quote": {"ask_cents": ask, "spread_cents": 2.0, "depth_contracts": 50.0,
                  "yes_bid_depth_contracts": 11.0, "yes_ask_depth_contracts": 22.0,
                  "no_bid_depth_contracts": 33.0, "no_ask_depth_contracts": 44.0,
                  "kalshi_depth_status": "ok",
                  "kalshi_depth_missing_reason": None,
                  "kalshi_depth_retry_used": True,
                  "spot_depth_status": "ok",
                  "spot_depth_missing_reason": None,
                  "spot_depth_source": "OKX HYPE-USDT",
                  "spot_depth_age_seconds": 4.0,
                  "spot_depth_bid_depth_levels": 1000.0,
                  "spot_depth_ask_depth_levels": 400.0,
                  "spot_depth_imbalance": 0.428571,
                  "spot_depth_trade_net_qty_15s": 12.0,
                  "quote_age_seconds": 1.0},
        "costs": {"fee_cents": 1.0, "total_cost_cents": 2.0},
        "regime": {"name": "TREND", "distance_sigma": 1.2},
        "manipulation": {"suspected": False}, "flip_risk": {"score": 20.0},
        "shadow_signals": {"order_flow_persistence": 0.05, "book_resiliency": 0.05,
                           "prediction_stability": 0.05},
        "snapshot_id": "snap-1",
    }
    a.update(extra)
    return a


def _runner(tmp_path, **over):
    base = dict(
        enabled=True, model_version="ultoim-v2", db_path=str(tmp_path / "u.sqlite3"),
        telegram_chat_id="", min_confidence=0.55, ask_lo=50.0, ask_hi=72.0,
        min_edge_cents=2.0, mark_band_seconds=90.0, reconcile_every_seconds=0.0,
        recap_every_seconds=0.0, max_spot_stale_seconds=8.0, min_scoreboard_n=30,
        # pin legacy defaults (owner live-defaults asserted in test_ultoim_v2.py)
        skip_15m=False, deliver_top_n=1, deliver_by_reward_risk=False, no_edge_waive=False,
        no_only=True, btc_confirm_enabled=False, require_inverse_edge=False, skip_7m=False,
    )
    base.update(over)
    cfg = UltoimV2Config(**base)
    r = UltoimV2Runner(cfg)
    r.telegram = _StubTelegram()
    return r


def _drow(**over):
    row = {
        "created_at": 1000.0, "model_version": "ultoim-v2", "asset": "BTC",
        "ticker": "T1", "interval": "10M", "window_key": 5, "mark_seconds": 600.0,
        "fired": 1, "predicted_side": "NO", "entry_ask_cents": 55.0,
        "delivery_status": "SENT", "distance_sigma": 1.2,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# distance_research_scoreboard
# --------------------------------------------------------------------------- #
def test_distance_research_scoreboard_splits_near_and_far(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # FAR (|sigma|>=0.15): two wins. NEAR-pin (|sigma|<0.15): one win, one loss.
    led.record_decision(_drow(ticker="F1", distance_sigma=1.20, entry_ask_cents=55.0))
    led.record_decision(_drow(ticker="F2", interval="7M", distance_sigma=-0.50, entry_ask_cents=60.0))
    led.record_decision(_drow(ticker="N1", interval="15M", distance_sigma=0.05, entry_ask_cents=52.0))
    led.record_decision(_drow(ticker="N2", interval="12M", distance_sigma=-0.10, entry_ask_cents=58.0))
    led.resolve("ultoim-v2", "F1", "NO", 9500.0)   # win +45
    led.resolve("ultoim-v2", "F2", "NO", 9500.0)   # win +40
    led.resolve("ultoim-v2", "N1", "NO", 9500.0)   # win +48
    led.resolve("ultoim-v2", "N2", "YES", 9500.0)  # loss -58

    sb = led.distance_research_scoreboard("ultoim-v2", pin_sigma=0.15)
    assert sb["available"] and sb["pin_sigma"] == 0.15
    assert sb["book"]["n"] == 4
    assert sb["far"]["n"] == 2 and sb["far"]["right"] == 2          # far = clean
    assert sb["near_pin"]["n"] == 2 and sb["near_pin"]["right"] == 1  # pin = mixed
    # far P&L (+85) beats near-pin (+48-58 = -10) — the transporting direction.
    assert sb["far"]["pnl_total_cents"] == pytest.approx(85.0)
    assert sb["near_pin"]["pnl_total_cents"] == pytest.approx(-10.0)
    # threshold is honoured: a tiny pin_sigma puts everything in 'far'.
    sb2 = led.distance_research_scoreboard("ultoim-v2", pin_sigma=0.0)
    assert sb2["far"]["n"] == 4 and sb2["near_pin"]["n"] == 0
    led.close()


def test_distance_scoreboard_only_settled_no_rows(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_drow(ticker="Y1", predicted_side="YES", fired=0, distance_sigma=1.0))
    led.record_decision(_drow(ticker="X1", distance_sigma=None))       # null distance excluded
    led.record_decision(_drow(ticker="N1", interval="7M", distance_sigma=0.4))
    led.resolve("ultoim-v2", "Y1", "NO", 9500.0)
    led.resolve("ultoim-v2", "N1", "NO", 9500.0)
    sb = led.distance_research_scoreboard("ultoim-v2")
    assert sb["book"]["n"] == 1                  # only the settled NO row with distance
    led.close()


def test_distance_research_scoreboard_splits_by_interval(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # 15M near (toxic loss), 15M far (win), 10M near (win), 7M far (win). No 10M-far/7M-near
    # so those buckets must report n=0 cleanly, and 7M has no near rows at all.
    led.record_decision(_drow(ticker="A", interval="15M", distance_sigma=0.05, entry_ask_cents=52.0))
    led.record_decision(_drow(ticker="B", interval="15M", distance_sigma=1.20, entry_ask_cents=55.0))
    led.record_decision(_drow(ticker="C", interval="10M", distance_sigma=-0.08, entry_ask_cents=60.0))
    led.record_decision(_drow(ticker="D", interval="7M", distance_sigma=0.90, entry_ask_cents=58.0))
    led.resolve("ultoim-v2", "A", "YES", 9500.0)  # 15M near loss -52
    led.resolve("ultoim-v2", "B", "NO", 9500.0)   # 15M far  win  +45
    led.resolve("ultoim-v2", "C", "NO", 9500.0)   # 10M near win  +40
    led.resolve("ultoim-v2", "D", "NO", 9500.0)   # 7M  far  win  +42

    sb = led.distance_research_scoreboard("ultoim-v2", pin_sigma=0.15)
    # back-compat: top-level keys unchanged and aggregate correctly across intervals.
    assert sb["available"] and sb["pin_sigma"] == 0.15
    assert sb["book"]["n"] == 4
    assert sb["near_pin"]["n"] == 2 and sb["near_pin"]["right"] == 1   # A(loss)+C(win)
    assert sb["far"]["n"] == 2 and sb["far"]["right"] == 2             # B+D both win

    bi = sb["by_interval"]
    assert set(bi) == {"15M", "10M", "7M"}
    # 15M: one near (loss), one far (win) — the masked-toxic near bucket is now visible.
    assert bi["15M"]["book"]["n"] == 2
    assert bi["15M"]["near_pin"]["n"] == 1 and bi["15M"]["near_pin"]["right"] == 0
    assert bi["15M"]["near_pin"]["pnl_total_cents"] == pytest.approx(-52.0)
    assert bi["15M"]["far"]["n"] == 1 and bi["15M"]["far"]["right"] == 1
    # 10M: only a near row; its far bucket is empty (n=0, not a KeyError).
    assert bi["10M"]["near_pin"]["n"] == 1 and bi["10M"]["near_pin"]["right"] == 1
    assert bi["10M"]["far"]["n"] == 0
    # 7M: only a far row; its near bucket is empty.
    assert bi["7M"]["far"]["n"] == 1 and bi["7M"]["far"]["right"] == 1
    assert bi["7M"]["near_pin"]["n"] == 0
    led.close()


def test_distance_by_interval_empty_interval_yields_zero_buckets(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # Only 15M rows: 10M and 7M must exist as all-zero bucket dicts, never KeyError.
    led.record_decision(_drow(ticker="A", interval="15M", distance_sigma=0.05))
    led.resolve("ultoim-v2", "A", "NO", 9500.0)
    sb = led.distance_research_scoreboard("ultoim-v2")
    assert sb["by_interval"]["15M"]["near_pin"]["n"] == 1
    for empty in ("10M", "7M"):
        for bucket in ("book", "near_pin", "far"):
            assert sb["by_interval"][empty][bucket]["n"] == 0   # present, not missing
    led.close()


# --------------------------------------------------------------------------- #
# recap surfaces the research screens (marker-safe)
# --------------------------------------------------------------------------- #
_FORBIDDEN = ("V9.5 CHECK", "ENTRY RECOMMENDED", "NO ENTRY YET", "Hourly Report —", "TOP 3 PICKS")


def _min_sb(**extra):
    agg0 = {"n": 0, "right": 0, "wrong": 0}
    sb = {
        "min_n": 30, "resolved": 5, "total_recorded": 5, "delivery_counts": {},
        "overall": {"n": 5, "right": 3, "wrong": 2, "accuracy": 0.6, "ci_low": 0.2,
                    "ci_high": 0.9, "low_n": True, "pnl_total_cents": 10.0,
                    "pnl_avg_cents": 2.0, "base_rate": 0.6, "base_rate_side": "NO",
                    "edge_over_base": 0.0},
        "by_interval": {iv: dict(agg0) for iv in ("15M", "10M", "7M")},
        "by_side": {"NO": dict(agg0), "YES": dict(agg0)},
        "by_regime_directional": {"NO_PRONE": dict(agg0), "YES_PRONE": dict(agg0),
                                  "BALANCED": dict(agg0)},
    }
    sb.update(extra)
    return sb


def test_recap_shows_research_screens():
    cfg = UltoimV2Config(enabled=True, min_scoreboard_n=30, min_promote_n=50)
    bucket = lambda n, acc, avg: {"n": n, "right": int(round(acc * n)), "wrong": n - int(round(acc * n)),
                                  "accuracy": acc, "ci_low": acc - 0.1, "ci_high": acc + 0.1,
                                  "pnl_total_cents": avg * n, "pnl_avg_cents": avg}
    sb = _min_sb(
        s15_research={"available": True, "version": "lukewarm-cheap-1",
                      "book": bucket(24, 0.67, 16.5), "gated": bucket(7, 1.0, 50.0),
                      "gated_with_cal_drift": bucket(7, 1.0, 50.0), "gated_with_fresh": {"n": 0}},
        distance_research={"available": True, "pin_sigma": 0.15,
                           "book": bucket(68, 0.71, 5.0), "near_pin": bucket(40, 0.60, -2.0),
                           "far": bucket(28, 0.86, 12.0),
                           "by_interval": {
                               "15M": {"book": bucket(20, 0.50, -31.0),
                                       "near_pin": bucket(12, 0.40, -31.0),
                                       "far": bucket(8, 0.88, 10.0)},
                               "10M": {"book": bucket(30, 0.80, 9.0),
                                       "near_pin": bucket(18, 0.75, 7.8),
                                       "far": bucket(12, 0.90, 11.0)},
                               "7M": {"book": {"n": 0}, "near_pin": {"n": 0}, "far": {"n": 0}}}},
    )
    text = panel.build_recap(sb, [], [], cfg)
    assert "15M screen — s15" in text and "would-fire" in text and "cal-drift" in text
    assert "Distance-to-strike" in text and "near (pin)" in text and "far  (keep)" in text
    assert "+50.0¢/pick" in text and "-2.0¢/pick" in text       # per-pick P&L rendered
    # per-interval block: 15M + 10M render (book n>0); empty 7M is omitted.
    assert "by interval" in text and "15M near:" in text and "10M near:" in text
    assert "7M near:" not in text and "7M far :" not in text
    assert "-31.0¢/pick" in text                               # the masked toxic 15M bucket
    assert "RESEARCH RECAP" in text
    for m in _FORBIDDEN:
        assert m not in text


def test_recap_omits_research_screens_when_empty():
    cfg = UltoimV2Config(enabled=True, min_scoreboard_n=30, min_promote_n=50)
    # available but book n=0 -> block suppressed (no noise before data exists).
    sb = _min_sb(s15_research={"available": True, "book": {"n": 0}},
                 distance_research={"available": True, "pin_sigma": 0.15, "book": {"n": 0}})
    text = panel.build_recap(sb, [], [], cfg)
    assert "15M screen — s15" not in text and "Distance-to-strike" not in text


# --------------------------------------------------------------------------- #
# observe(): per-asset isolation (never raises / never drops the whole cycle)
# --------------------------------------------------------------------------- #
def test_observe_isolates_malformed_asset(tmp_path):
    r = _runner(tmp_path)
    captured: dict = {}
    r._jobs.put_nowait = lambda item: captured.update(item[1])   # type: ignore
    r._ensure_worker = lambda: None                              # type: ignore
    good = _analysis(side="NO")
    bad = _analysis(side="NO")
    bad["structural"] = [1, 2, 3]      # structural.get("z_score") raises -> skip this asset
    # Must NOT raise, and must keep the good asset.
    r.observe(analyses={"BTC": good, "ETH": bad},
              canonicals={"BTC": _canon("T-BTC", 900.0, 9000.0),
                          "ETH": _canon("T-ETH", 900.0, 9000.0)}, now=1000.0)
    cands = captured.get("candidates", [])
    assert {c["asset"] for c in cands} == {"BTC"}      # ETH isolated, BTC survives


def test_extract_candidate_skips_when_not_in_data(tmp_path):
    r = _runner(tmp_path)
    # missing canonical -> None; no prediction -> None.
    assert r._extract_candidate("BTC", _analysis(), None, None) is None
    assert r._extract_candidate("BTC", {"prediction_available": False},
                                _canon("T", 900.0, 9000.0), None) is None
    cand = r._extract_candidate("BTC", _analysis(), _canon("T-BTC", 900.0, 9000.0), None)
    assert cand is not None and cand["asset"] == "BTC" and cand["predicted_side"] == "NO"
    assert cand["yes_bid_depth_contracts"] == pytest.approx(11.0)
    assert cand["yes_ask_depth_contracts"] == pytest.approx(22.0)
    assert cand["no_bid_depth_contracts"] == pytest.approx(33.0)
    assert cand["no_ask_depth_contracts"] == pytest.approx(44.0)
    assert cand["kalshi_depth_status"] == "ok"
    assert cand["kalshi_depth_retry_used"] is True
    assert cand["spot_depth_status"] == "ok"
    assert cand["spot_depth_imbalance"] == pytest.approx(0.428571)
    assert cand["spot_depth_trade_net_qty_15s"] == pytest.approx(12.0)


def test_v2_records_full_side_depth_without_gating(tmp_path):
    r = _runner(tmp_path)
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    c = {"BTC": _canon("T-BTC", 900.0, 9000.0)}
    cand = r._extract_candidate("BTC", a["BTC"], c["BTC"], None)
    r._observe_sync(candidates=[cand], now=1000.0)
    row = r.ledger.recent_rows("ultoim-v2", limit=1)[0]
    assert row["fired"] == 1
    assert row["yes_bid_depth_contracts"] == pytest.approx(11.0)
    assert row["yes_ask_depth_contracts"] == pytest.approx(22.0)
    assert row["no_bid_depth_contracts"] == pytest.approx(33.0)
    assert row["no_ask_depth_contracts"] == pytest.approx(44.0)
    assert row["kalshi_depth_status"] == "ok"
    assert row["kalshi_depth_retry_used"] == 1
    assert row["spot_depth_status"] == "ok"
    assert row["spot_depth_source"] == "OKX HYPE-USDT"
    assert row["spot_depth_bid_depth_levels"] == pytest.approx(1000.0)
    assert row["spot_depth_ask_depth_levels"] == pytest.approx(400.0)
    assert row["spot_depth_imbalance"] == pytest.approx(0.428571)


# --------------------------------------------------------------------------- #
# the REAL worker loop records an observe job (the previously-untested surface)
# --------------------------------------------------------------------------- #
def test_worker_loop_processes_real_observe_job(tmp_path):
    r = _runner(tmp_path)                      # real _ensure_worker + queue + thread
    a = {"BTC": _analysis(side="NO", sel=0.65, ask=60.0, net_edge=5.0, mkt_yes=0.35)}
    c = {"BTC": _canon("T-BTC", 900.0, 9000.0)}
    r.observe(analyses=a, canonicals=c, now=1000.0)
    r._jobs.join()                             # block until the worker drains the job
    sb = r.scoreboard()
    assert sb["all_observations"]["recorded"] == 1
    assert sb["all_observations"]["fired"] == 1
    assert r.telegram.sent and "ULTOIM V2" in r.telegram.sent[0]


# --------------------------------------------------------------------------- #
# flow-against-NO research scoreboard + recap (the #1 loss-forensics fix, record-only)
# --------------------------------------------------------------------------- #
def _bucket(n, acc, avg):
    right = int(round(acc * n))
    return {"n": n, "right": right, "wrong": n - right, "accuracy": acc,
            "ci_low": max(0.0, acc - 0.1), "ci_high": min(1.0, acc + 0.1),
            "pnl_total_cents": avg * n, "pnl_avg_cents": avg}


def test_flow_research_scoreboard_flow_and_regime_cuts(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # flow>=0.6 = would-ABSTAIN (NO against strong buy flow). Make those the losers.
    led.record_decision(_drow(ticker="A", champion_flow=0.80, regime_directional="YES_PRONE",
                              entry_ask_cents=55.0))
    led.record_decision(_drow(ticker="B", interval="7M", champion_flow=0.70,
                              regime_directional="YES_PRONE", entry_ask_cents=58.0))
    led.record_decision(_drow(ticker="C", interval="15M", champion_flow=0.10,
                              regime_directional="NO_PRONE", entry_ask_cents=52.0))
    led.record_decision(_drow(ticker="D", interval="12M", champion_flow=None,
                              regime_directional="BALANCED", entry_ask_cents=50.0))
    led.resolve("ultoim-v2", "A", "YES", 9500.0)   # NO loss -55 (high flow -> abstain was right)
    led.resolve("ultoim-v2", "B", "YES", 9500.0)   # NO loss -58
    led.resolve("ultoim-v2", "C", "NO", 9500.0)    # NO win +48 (low flow -> kept)
    led.resolve("ultoim-v2", "D", "NO", 9500.0)    # NO win +50 (None flow -> kept)

    sb = led.flow_research_scoreboard("ultoim-v2", flow_threshold=0.6)
    assert sb["available"] and sb["flow_threshold"] == 0.6 and sb["book"]["n"] == 4
    # champion_flow cut: the two high-flow rows are the would-abstain losers.
    assert sb["flow_abstain"]["n"] == 2 and sb["flow_abstain"]["right"] == 0
    assert sb["flow_abstain"]["pnl_total_cents"] == pytest.approx(-113.0)
    assert sb["flow_keep"]["n"] == 2 and sb["flow_keep"]["right"] == 2      # incl. None-flow (kept)
    # v2-native regime proxy cut (has data without champion_flow recorded).
    assert sb["regime_abstain"]["n"] == 2 and sb["regime_abstain"]["right"] == 0
    assert sb["regime_keep"]["n"] == 2 and sb["regime_keep"]["right"] == 2
    led.close()


def test_extract_candidate_captures_champion_flow(tmp_path):
    r = _runner(tmp_path)
    a = _analysis()
    a["feature_values"] = {"flow": 0.72, "threshold_interaction": 0.3}
    cand = r._extract_candidate("BTC", a, _canon("T-BTC", 900.0, 9000.0), None)
    assert cand["champion_flow"] == pytest.approx(0.72)
    assert cand["threshold_interaction"] == pytest.approx(0.3)
    # absent feature_values -> None (no crash, never gates)
    cand2 = r._extract_candidate("BTC", _analysis(), _canon("T-BTC", 900.0, 9000.0), None)
    assert cand2["champion_flow"] is None


def test_recap_shows_flow_research():
    cfg = UltoimV2Config(enabled=True, min_scoreboard_n=30, min_promote_n=50)
    sb = _min_sb(flow_research={
        "available": True, "flow_threshold": 0.6, "book": _bucket(68, 0.71, 5.0),
        "flow_keep": _bucket(60, 0.75, 8.0), "flow_abstain": _bucket(8, 0.30, -15.0),
        "regime_keep": _bucket(61, 0.74, 6.0), "regime_abstain": _bucket(7, 0.0, -16.0)})
    text = panel.build_recap(sb, [], [], cfg)
    assert "Flow-against-NO" in text and "abstain (flow)" in text and "regime proxy" in text
    assert "-15.0¢/pick" in text                      # the would-abstain bleed is shown
    for m in _FORBIDDEN:
        assert m not in text
