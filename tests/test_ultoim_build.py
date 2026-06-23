"""Tests for the Ultoim Build read-only research overlay.

Deterministic: no network, no live exchanges, no worker-thread timing (the sync
handlers _observe_sync / _reconcile_sync are driven directly). Verifies the
separate-records contract, value ranking, multi-factor grading, flip research,
settlement grading, duplicate prevention, delivery-status accounting, and
restart safety.
"""
from __future__ import annotations

import types

import pytest

from q15_upgrade.ultoim import config as cfg_mod
from q15_upgrade.ultoim import flip_research, ranker
from q15_upgrade.ultoim.config import UltoimConfig
from q15_upgrade.ultoim.ledger import UltoimLedger, _window_key
from q15_upgrade.ultoim.runner import UltoimRunner, get_runner


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _StubTelegram:
    def __init__(self, delivered=True, message_id=555):
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


def _canon(ticker, secs, close):
    return types.SimpleNamespace(ticker=ticker, seconds_remaining=secs, settlement_time=close)


def _analysis(side="NO", sel=0.75, ask=40.0, dq=0.8, eq=0.8, yes=0.25, chal=0.27,
              base=0.24, net_edge=3.0, manip=False, flip_score=20.0,
              ofp=0.05, book=0.05, stab=0.05):
    return {
        "prediction_available": True,
        "prediction_side": side,
        "selected_probability": sel,
        "conservative_probability": sel - 0.03,
        "yes_probability": yes,
        "challenger_yes_probability": chal,
        "baseline_yes_probability": base,
        "data_quality": dq,
        "evidence_quality": eq,
        "net_edge_cents": net_edge,
        "quote": {"ask_cents": ask},
        "manipulation": {"suspected": manip},
        "flip_risk": {"score": flip_score},
        "shadow_signals": {"order_flow_persistence": ofp, "book_resiliency": book,
                           "prediction_stability": stab},
    }


def _config(tmp_path, **env):
    return UltoimConfig(
        enabled=True, model_version="ultoim-build-v1",
        db_path=str(tmp_path / "ultoim.sqlite3"),
        telegram_chat_id="", mark_band_seconds=90.0, top_k=3,
        reconcile_every_seconds=0.0, edge_weight=0.15, edge_scale_cents=10.0,
        grade_a_min=0.60, grade_b_min=0.48, flip_grade_weight=0.20,
        flip_decision_threshold=0.55, **env,
    )


def _runner(tmp_path, telegram=None):
    r = UltoimRunner(_config(tmp_path))
    if telegram is not None:
        r.telegram = telegram
    return r


# --------------------------------------------------------------------------- #
# enable gating
# --------------------------------------------------------------------------- #
def test_enabled_by_default(monkeypatch):
    # The conftest autouse fixture forces it off for the suite; clear that to see
    # the real production default.
    monkeypatch.delenv("Q15_ULTOIM_ENABLED", raising=False)
    cfg_mod.reset_enabled_cache()
    assert UltoimConfig.from_env().enabled is True
    cfg_mod.reset_enabled_cache()


def test_explicit_disable_returns_no_runner(monkeypatch):
    monkeypatch.setenv("Q15_ULTOIM_ENABLED", "false")
    cfg_mod.reset_enabled_cache()
    assert UltoimConfig.from_env().enabled is False
    assert get_runner() is None
    cfg_mod.reset_enabled_cache()


# --------------------------------------------------------------------------- #
# ranker
# --------------------------------------------------------------------------- #
def test_model_agreement_spread():
    assert ranker.model_agreement([0.5, 0.5, 0.5]) == 1.0
    assert ranker.model_agreement([0.3, 0.8]) == 0.0      # full spread
    assert 0.0 < ranker.model_agreement([0.5, 0.6]) < 1.0


def test_quality_penalties(tmp_path):
    cfg = _config(tmp_path)
    base = ranker.quality_score(_clean_pick(), None, cfg)
    manip = ranker.quality_score(_clean_pick(manipulation_suspected=True), None, cfg)
    assert manip < base                                   # manipulation lowers quality
    yes_lowsig = ranker.quality_score(
        _clean_pick(predicted_side="YES", order_flow_persistence=-0.1, book_resiliency=-0.1),
        None, cfg)
    yes_goodsig = ranker.quality_score(
        _clean_pick(predicted_side="YES", order_flow_persistence=0.1, book_resiliency=0.1),
        None, cfg)
    assert yes_lowsig < yes_goodsig                       # YES veto bites the weak-signal YES


def test_grade_thresholds(tmp_path):
    cfg = _config(tmp_path)
    assert ranker.grade_for(0.65, cfg) == "A"
    assert ranker.grade_for(0.50, cfg) == "B"
    assert ranker.grade_for(0.30, cfg) == "C"


def test_value_rank_orders_and_caps(tmp_path):
    cfg = _config(tmp_path)
    picks = [
        {"asset": "A", "quality_score": 0.5, "net_edge_cents": -5.0},
        {"asset": "B", "quality_score": 0.5, "net_edge_cents": 8.0},   # value lifts B
        {"asset": "C", "quality_score": 0.9, "net_edge_cents": 0.0},
        {"asset": "D", "quality_score": 0.2, "net_edge_cents": 0.0},
    ]
    top = ranker.value_rank(picks, cfg)
    assert [p["asset"] for p in top] == ["C", "B", "A"]   # top_k=3, B beats A on edge
    assert [p["rank"] for p in top] == [1, 2, 3]
    assert all(p.get("confidence_grade") in ("A", "B", "C") for p in top)


def test_grade_b_default_recalibrated(monkeypatch):
    # Recalibrated default cutoffs that match the weighted-average scorer's range.
    for k in ("Q15_ULTOIM_GRADE_A_MIN", "Q15_ULTOIM_GRADE_B_MIN"):
        monkeypatch.delenv(k, raising=False)
    cfg = UltoimConfig()
    assert cfg.grade_a_min == 0.60 and cfg.grade_b_min == 0.50


def test_grade_not_pinned_to_C_strong_pick_reaches_A(tmp_path):
    # Regression for the "always C" bug: under the old all-multiplicative scorer a
    # strong setup still collapsed under the B line. The weighted average lets it
    # reach A.
    cfg = _config(tmp_path)
    strong = _clean_pick(predicted_side="NO", calibrated_yes_probability=0.10,
                         baseline_yes_probability=0.12, data_quality=0.95,
                         evidence_quality=0.92)
    q = ranker.quality_score(strong, None, cfg)
    assert q >= cfg.grade_a_min
    assert ranker.grade_for(q, cfg) == "A"


def test_weak_pick_is_C(tmp_path):
    cfg = _config(tmp_path)
    weak = _clean_pick(predicted_side="NO", calibrated_yes_probability=0.48,
                       baseline_yes_probability=0.50, data_quality=0.6,
                       evidence_quality=0.6)
    q = ranker.quality_score(weak, None, cfg)
    assert q < cfg.grade_b_min
    assert ranker.grade_for(q, cfg) == "C"


def test_grade_monotonic_in_confidence(tmp_path):
    # For a NO pick, a lower YES probability = higher chosen-side confidence =
    # higher grade. Confidence is the dominant term, derived from the CALIBRATED
    # probability (fix #3), so the grade tracks it.
    cfg = _config(tmp_path)

    def q(cal_yes):
        return ranker.quality_score(
            _clean_pick(predicted_side="NO", calibrated_yes_probability=cal_yes,
                        baseline_yes_probability=cal_yes + 0.02), None, cfg)

    assert q(0.45) < q(0.30) < q(0.15)


def test_challenger_excluded_from_agreement_by_default(tmp_path):
    # A wildly divergent (poorly-calibrated) challenger must not drag the grade
    # unless explicitly folded in (fix #4).
    cfg_excl = _config(tmp_path)
    cfg_incl = _config(tmp_path, grade_includes_challenger=True)
    pick = _clean_pick(predicted_side="NO", calibrated_yes_probability=0.25,
                       baseline_yes_probability=0.24,
                       challenger_yes_probability=0.90)
    q_excl = ranker.quality_score(pick, None, cfg_excl)
    q_incl = ranker.quality_score(pick, None, cfg_incl)
    assert q_excl > q_incl
    assert ranker.grade_for(q_excl, cfg_excl) == "A"     # excluded: strong pick stays A


def _clean_pick(**over):
    pick = {
        "predicted_side": "NO", "selected_probability": 0.75, "data_quality": 0.8,
        "evidence_quality": 0.8, "calibrated_yes_probability": 0.25,
        "challenger_yes_probability": 0.26, "baseline_yes_probability": 0.24,
        "manipulation_suspected": False, "order_flow_persistence": 0.05,
        "book_resiliency": 0.05,
    }
    pick.update(over)
    return pick


# --------------------------------------------------------------------------- #
# flip research
# --------------------------------------------------------------------------- #
def test_flip_probability_bounded_and_none():
    assert flip_research.ensemble_flip_probability({}) is None
    p = flip_research.ensemble_flip_probability(
        {"flip_risk_score": 80.0, "prediction_stability": -0.2,
         "book_resiliency": -0.2, "order_flow_persistence": -0.2})
    assert 0.0 <= p <= 1.0 and p > 0.5                    # high risk + weak signals -> high
    q = flip_research.ensemble_flip_probability(
        {"flip_risk_score": 5.0, "prediction_stability": 0.3,
         "book_resiliency": 0.3, "order_flow_persistence": 0.3})
    assert q < p


def test_flip_fields_decision(tmp_path):
    cfg = _config(tmp_path)
    hi = flip_research.flip_fields("YES", 0.9, cfg)
    assert hi["flip_decision"] == "YES" and hi["expected_flip_side"] == "NO"
    lo = flip_research.flip_fields("YES", 0.1, cfg)
    assert lo["flip_decision"] == "NO" and lo["expected_flip_side"] == "YES"
    none = flip_research.flip_fields("NO", None, cfg)
    assert none["flip_decision"] == "NO"


# --------------------------------------------------------------------------- #
# ledger: record / lock / resolve / scoreboard
# --------------------------------------------------------------------------- #
def _row(**over):
    row = {
        "created_at": 1000.0, "model_version": "ultoim-build-v1", "asset": "BTC",
        "ticker": "T-BTC", "interval": "10M", "window_key": 5, "rank": 1,
        "predicted_side": "NO", "outcome_pct": 75.0, "selected_yes_probability": 0.25,
        "conservative_probability": 0.72, "net_edge_cents": 3.0, "entry_ask_cents": 40.0,
        "quality_score": 0.7, "confidence_grade": "A", "data_quality": 0.8,
        "evidence_quality": 0.8, "model_agreement": 0.9, "manipulation_suspected": 0,
        "flip_probability": 0.2, "flip_decision": "NO", "original_predicted_side": "NO",
        "expected_flip_side": "NO", "order_flow_persistence": 0.05, "book_resiliency": 0.05,
        "prediction_stability": 0.05, "close_time": 9000.0, "snapshot_id": "10M@1000",
        "delivery_status": "SENT",
    }
    row.update(over)
    return row


def test_record_pick_idempotent(tmp_path):
    led = UltoimLedger(str(tmp_path / "u.sqlite3"))
    assert led.record_pick(_row()) is not None
    assert led.record_pick(_row()) is None                # dup (version, ticker, interval)
    assert led.record_pick(_row(interval="7M")) is not None
    led.close()


def test_lock_dedup(tmp_path):
    led = UltoimLedger(str(tmp_path / "u.sqlite3"))
    assert led.lock_report("ultoim-build-v1", "12M", 5, 1.0) is True
    assert led.lock_report("ultoim-build-v1", "12M", 5, 1.0) is False
    assert led.report_locked("ultoim-build-v1", "12M", 5) is True
    assert led.report_locked("ultoim-build-v1", "12M", 6) is False
    led.close()


def test_resolve_grades_and_pnl(tmp_path):
    led = UltoimLedger(str(tmp_path / "u.sqlite3"))
    led.record_pick(_row(ticker="T1", predicted_side="NO", entry_ask_cents=40.0))   # NO, settles NO -> win
    led.record_pick(_row(ticker="T2", interval="7M", predicted_side="YES",
                         original_predicted_side="YES", entry_ask_cents=60.0))      # YES, settles NO -> loss + flip
    assert led.resolve("ultoim-build-v1", "T1", "NO", 9500.0) == 1
    assert led.resolve("ultoim-build-v1", "T2", "NO", 9500.0) == 1
    assert led.resolve("ultoim-build-v1", "T1", "NO", 9600.0) == 0   # idempotent re-grade

    sb = led.scoreboard("ultoim-build-v1")
    assert sb["resolved"] == 2
    assert sb["overall"]["right"] == 1 and sb["overall"]["wrong"] == 1
    # T1 win: +60c (100-40); T2 loss: -60c -> total 0
    assert sb["overall"]["pnl_total_cents"] == 0.0
    # T2 was an outcome flip (orig YES, settled NO); flip_decision was NO -> wrong call
    assert sb["flip"]["actual_flips"] == 1
    led.close()


def test_scoreboard_delivered_only(tmp_path):
    led = UltoimLedger(str(tmp_path / "u.sqlite3"))
    led.record_pick(_row(ticker="S1", delivery_status="SENT"))
    led.record_pick(_row(ticker="S2", interval="7M", delivery_status="DELIVERY_FAILED"))
    led.resolve("ultoim-build-v1", "S1", "NO", 9500.0)
    led.resolve("ultoim-build-v1", "S2", "NO", 9500.0)
    visible = led.scoreboard("ultoim-build-v1", delivered_only=True)
    everything = led.scoreboard("ultoim-build-v1", delivered_only=False)
    assert visible["resolved"] == 1                        # delivery-failed excluded
    assert everything["resolved"] == 2
    assert visible["delivery_counts"].get("DELIVERY_FAILED") == 1
    led.close()


# --------------------------------------------------------------------------- #
# runner end-to-end (sync handlers, stub telegram, fake resolver)
# --------------------------------------------------------------------------- #
def test_observe_fires_one_report_with_three_picks(tmp_path):
    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg)
    assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
    analyses = {a: _analysis(sel=0.60 + i * 0.03, net_edge=float(i))
                for i, a in enumerate(assets)}
    canonicals = {a: _canon(f"T-{a}", secs=715.0, close=9000.0) for a in assets}

    # observe() extracts + enqueues; drive the sync handler deterministically.
    cands = _extract(r, analyses, canonicals, now=1000.0)
    r._observe_sync(candidates=cands, now=1000.0)

    sb = r.scoreboard()
    assert sb["total_recorded"] == 3                        # exactly top-3
    assert len(tg.sent) == 1 and "ULTOIM BUILD · 12M" in tg.sent[0]
    assert r.ledger.report_locked("ultoim-build-v1", "12M", _window_key(9000.0, 1000.0))

    # second observe in the same window+interval is a no-op (dedup)
    r._observe_sync(candidates=cands, now=1001.0)
    assert r.scoreboard()["total_recorded"] == 3
    assert len(tg.sent) == 1


def test_observe_only_in_band(tmp_path):
    r = _runner(tmp_path, telegram=_StubTelegram())
    assets = ["BTC", "ETH", "SOL"]
    analyses = {a: _analysis() for a in assets}
    # 500s left: not in any band ([630,720],[510,600],[330,420])
    canonicals = {a: _canon(f"T-{a}", secs=500.0, close=9000.0) for a in assets}
    r._observe_sync(candidates=_extract(r, analyses, canonicals, now=1000.0), now=1000.0)
    assert r.scoreboard()["total_recorded"] == 0


def test_runner_reconcile_grades_against_resolver(tmp_path):
    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg)
    assets = ["BTC", "ETH", "SOL"]
    analyses = {"BTC": _analysis(side="NO", sel=0.8),
                "ETH": _analysis(side="YES", sel=0.7),
                "SOL": _analysis(side="NO", sel=0.65)}
    canonicals = {a: _canon(f"T-{a}", secs=715.0, close=9000.0) for a in assets}
    r._observe_sync(candidates=_extract(r, analyses, canonicals, now=1000.0), now=1000.0)

    class _Resolver:
        def get_market(self, ticker):
            return {"result": "NO"}                         # everything settles NO

    r._reconcile_sync(resolver=_Resolver(), now=9500.0)
    sb = r.scoreboard()
    assert sb["resolved"] == 3
    # NO picks correct, the YES pick wrong (if ETH made the top-3)
    assert sb["overall"]["right"] >= 1


def test_restart_safety_no_double_record_or_grade(tmp_path):
    db = str(tmp_path / "persist.sqlite3")
    led = UltoimLedger(db)
    led.record_pick(_row(ticker="R1"))
    led.resolve("ultoim-build-v1", "R1", "NO", 9500.0)
    led.close()
    # reopen (simulates restart)
    led2 = UltoimLedger(db)
    assert led2.record_pick(_row(ticker="R1")) is None      # unique survives restart
    assert led2.resolve("ultoim-build-v1", "R1", "NO", 9600.0) == 0  # already graded
    assert led2.unresolved_closed("ultoim-build-v1", 9999.0) == []
    led2.close()


def _extract(runner, analyses, canonicals, now):
    """Run the synchronous extraction observe() does, capturing the candidates."""
    captured = {}
    orig = runner._jobs.put_nowait
    runner._jobs.put_nowait = lambda item: captured.update(item[1])  # type: ignore
    try:
        # _ensure_worker would start a thread; stub it out for determinism.
        runner._ensure_worker = lambda: None  # type: ignore
        runner.observe(analyses=analyses, canonicals=canonicals, now=now)
    finally:
        runner._jobs.put_nowait = orig  # type: ignore
    return captured.get("candidates", [])
