"""Tests for the two new V3 books.

Book 1 (warn_flip_entry): follow a confirmed ultoim_v2 exit-warning flip when the
flip side's live ask is inside the pre-registered 55-75c band.
Book 2 (fav_10m): buy the predicted (favorite) side at the 10M mark inside the
85-90c band; shadow-first (NOTIFY behind its own flag).

Both are default-OFF, paper-only, and self-governed (empirical Wilson-LB EV once
n>=30, auto-mute governor).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import pytest

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_FAV_10M,
    BOT_WARN_FLIP,
    REJECTED,
    STRATEGY_VERSION,
    decisions_for_row,
    fav_10m_decision,
    warn_flip_entry_decision,
)
from q15_upgrade.strategy_bots.telegram import (
    build_fav_10m_alert,
    build_v3_alert,
    build_warn_flip_alert,
)


def _warn_row(**over):
    base = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "BTC",
        "ticker": "KXBTC-WF",
        "interval": "WARN",
        "window_key": 42,
        "close_time": 1500.0,
        "record_kind": "EXIT_WARNING_FLIP",
        "delivery_status": "RECORDED",
        "predicted_side": "NO",
        "entry_ask_cents": 67.0,
        "spread_cents": 2.0,
        "warn_seconds_remaining": 305.0,
        "entry_side": "YES",
        "entry_interval": "10M",
        "confirm_cycles": 6,
        "confirm_span_seconds": 21.0,
        "exit_value_cents": 30.0,
    }
    base.update(over)
    return base


def _fav_row(**over):
    base = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "ETH",
        "ticker": "KXETH-10M",
        "interval": "10M",
        "window_key": 77,
        "close_time": 1600.0,
        "record_kind": "INTERVAL_RESEARCH_10M",
        "delivery_status": "RECORDED",
        "predicted_side": "YES",
        "calibrated_yes_probability": 0.88,
        "entry_ask_cents": 87.0,
        "spread_cents": 2.0,
        "manipulation_suspected": False,
    }
    base.update(over)
    return base


class _Telegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}


# -- Book 1: warn_flip_entry rules ------------------------------------------------

def test_warn_flip_disabled_returns_none():
    os.environ.pop("Q15_V3_WARN_FLIP", None)
    assert warn_flip_entry_decision(_warn_row(), source_system="ultoim_v2") is None


def test_warn_flip_requires_exit_warning_record_kind(monkeypatch):
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    row = _warn_row(record_kind="DELIVERED_CANDIDATE")
    assert warn_flip_entry_decision(row, source_system="ultoim_v2") is None


def test_warn_flip_band_gate_matrix(monkeypatch):
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")

    prime = warn_flip_entry_decision(_warn_row(entry_ask_cents=67.0), source_system="ultoim_v2")
    assert prime is not None and prime.decision_status == ACCEPTED
    assert prime.bot_name == BOT_WARN_FLIP
    assert "TIER_PRIME" in prime.reason_codes
    assert prime.threshold_profile["chase_max_cents"] == pytest.approx(68.0)
    assert prime.entry_ask_cents == 67.0 and prime.use_entry_ask_override
    assert prime.side_override == "NO"
    assert prime.original_source_side == "YES"

    edge = warn_flip_entry_decision(_warn_row(entry_ask_cents=72.0), source_system="ultoim_v2")
    assert edge is not None and edge.decision_status == ACCEPTED
    assert "TIER_EDGE" in edge.reason_codes

    above = warn_flip_entry_decision(_warn_row(entry_ask_cents=80.0), source_system="ultoim_v2")
    assert above is not None and above.decision_status == REJECTED
    assert "ABOVE_BAND" in above.reason_codes

    below = warn_flip_entry_decision(_warn_row(entry_ask_cents=50.0), source_system="ultoim_v2")
    assert below is not None and below.decision_status == REJECTED
    assert "BELOW_BAND" in below.reason_codes

    missing = warn_flip_entry_decision(_warn_row(entry_ask_cents=None), source_system="ultoim_v2")
    assert missing is not None and "FLIP_ASK_MISSING" in missing.reason_codes


def test_warn_flip_staleness_gate_only_when_configured(monkeypatch):
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    # default: ungated — a late warning still passes the band rule
    late = warn_flip_entry_decision(_warn_row(warn_seconds_remaining=90.0), source_system="ultoim_v2")
    assert late is not None and late.decision_status == ACCEPTED

    monkeypatch.setenv("Q15_V3_WARN_FLIP_MIN_SECONDS", "200")
    stale = warn_flip_entry_decision(_warn_row(warn_seconds_remaining=90.0), source_system="ultoim_v2")
    assert stale is not None and stale.decision_status == REJECTED
    assert "STALE_WARNING" in stale.reason_codes
    fresh = warn_flip_entry_decision(_warn_row(warn_seconds_remaining=305.0), source_system="ultoim_v2")
    assert fresh is not None and fresh.decision_status == ACCEPTED
    assert "FRESH_WARNING" in fresh.reason_codes


def test_warn_flip_empirical_ev_and_auto_mute(monkeypatch):
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    # below n=30 the discovery prior drives the EV estimate
    prior = warn_flip_entry_decision(_warn_row(), source_system="ultoim_v2")
    assert prior.threshold_profile["ev_prior_source"] == "discovery_n58"

    empirical = warn_flip_entry_decision(
        _warn_row(warn_flip_resolved_n=40, warn_flip_wilson_lb=0.74, warn_flip_accuracy=0.85),
        source_system="ultoim_v2",
    )
    assert empirical.threshold_profile["ev_prior_source"] == "empirical_wilson_lb"
    assert empirical.threshold_profile["ev_win_probability"] == pytest.approx(0.74)

    muted = warn_flip_entry_decision(
        _warn_row(warn_flip_resolved_n=90, warn_flip_wilson_lb=0.60, warn_flip_accuracy=0.68),
        source_system="ultoim_v2",
    )
    assert muted.decision_status == ACCEPTED  # recording continues
    assert muted.threshold_profile["auto_mute_active"] is True


# -- Book 2: fav_10m rules ---------------------------------------------------------

def test_fav_10m_disabled_and_interval_gates(monkeypatch):
    os.environ.pop("Q15_V3_FAV10M", None)
    assert fav_10m_decision(_fav_row(), source_system="ultoim_v2") is None
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    assert fav_10m_decision(_fav_row(interval="13M"), source_system="ultoim_v2") is None
    assert fav_10m_decision(_fav_row(), source_system="high_vol_flip") is None


def test_fav_10m_band_and_spread_gates(monkeypatch):
    monkeypatch.setenv("Q15_V3_FAV10M", "true")

    accepted = fav_10m_decision(_fav_row(), source_system="ultoim_v2")
    assert accepted is not None and accepted.decision_status == ACCEPTED
    assert accepted.bot_name == BOT_FAV_10M
    assert {"BAND_OK", "SPREAD_OK"}.issubset(set(accepted.reason_codes))
    assert accepted.entry_ask_cents == 87.0 and accepted.use_entry_ask_override

    low = fav_10m_decision(_fav_row(entry_ask_cents=80.0), source_system="ultoim_v2")
    assert low.decision_status == REJECTED and "OUT_OF_BAND" in low.reason_codes
    high = fav_10m_decision(_fav_row(entry_ask_cents=92.0), source_system="ultoim_v2")
    assert high.decision_status == REJECTED and "OUT_OF_BAND" in high.reason_codes

    wide = fav_10m_decision(_fav_row(spread_cents=9.0), source_system="ultoim_v2")
    assert wide.decision_status == REJECTED and "SPREAD_TOO_WIDE" in wide.reason_codes
    open_spread = fav_10m_decision(_fav_row(spread_cents=None), source_system="ultoim_v2")
    assert open_spread.decision_status == ACCEPTED
    assert "SPREAD_MISSING_FAIL_OPEN" in open_spread.reason_codes


def test_fav_10m_auto_mute_needs_power(monkeypatch):
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    # n=120 with a weak LB must NOT mute (below the 300-row power floor)
    early = fav_10m_decision(
        _fav_row(fav_10m_resolved_n=120, fav_10m_wilson_lb=0.80, fav_10m_accuracy=0.86),
        source_system="ultoim_v2",
    )
    assert early.threshold_profile["auto_mute_active"] is False
    late = fav_10m_decision(
        _fav_row(fav_10m_resolved_n=320, fav_10m_wilson_lb=0.85, fav_10m_accuracy=0.88),
        source_system="ultoim_v2",
    )
    assert late.threshold_profile["auto_mute_active"] is True


def test_decisions_for_row_includes_fav_10m_only_when_enabled(monkeypatch):
    row = _fav_row()
    os.environ.pop("Q15_V3_FAV10M", None)
    bots = {d.bot_name for d in decisions_for_row(row, source_system="ultoim_v2")}
    assert BOT_FAV_10M not in bots
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    bots = {d.bot_name for d in decisions_for_row(row, source_system="ultoim_v2")}
    assert BOT_FAV_10M in bots


# -- runtime: recording + notification --------------------------------------------

def _reset_runtime(tmp_path, monkeypatch, telegram):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    runtime._ledger = None
    runtime._telegram = telegram


def test_record_exit_warning_row_records_without_notify(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    os.environ.pop("Q15_V3_WARN_FLIP_NOTIFY", None)

    row_id = runtime.record_exit_warning_row(_warn_row())
    assert row_id is not None
    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_WARN_FLIP]
    assert len(rows) == 1
    assert rows[0]["decision_status"] == ACCEPTED
    assert rows[0]["side"] == "NO"
    assert rows[0]["entry_ask_cents"] == 67.0
    assert rows[0]["record_kind"] == "EXIT_WARNING_FLIP"
    assert tg.sent == []  # NOTIFY defaults off


def test_record_exit_warning_row_sends_new_ui_when_enabled(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    monkeypatch.setenv("Q15_V3_WARN_FLIP_NOTIFY", "true")

    row_id = runtime.record_exit_warning_row(_warn_row())
    assert row_id is not None
    led = runtime.get_ledger()
    row = led.row_by_id(row_id)
    assert row["notification_status"] == "SENT"
    assert len(tg.sent) == 1
    text = tg.sent[0]
    assert "V3 WARN-FLIP ENTRY" in text
    assert "BUY NO @ 67c" in text
    assert "chase ≤ 68c" in text
    assert "KXBTC-WF" in text


def test_record_exit_warning_rejected_rows_recorded_never_notified(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "true")
    monkeypatch.setenv("Q15_V3_WARN_FLIP_NOTIFY", "true")

    runtime.record_exit_warning_row(_warn_row(entry_ask_cents=82.0))
    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_WARN_FLIP]
    assert len(rows) == 1 and rows[0]["decision_status"] == REJECTED
    assert tg.sent == []


def test_record_exit_warning_disabled_book_is_noop(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    os.environ.pop("Q15_V3_WARN_FLIP", None)
    assert runtime.record_exit_warning_row(_warn_row()) is None
    led = runtime.get_ledger()
    assert [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_WARN_FLIP] == []


def test_fav_10m_notify_gating_and_message(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    os.environ.pop("Q15_V3_FAV10M_NOTIFY", None)

    assert runtime.record_source_row(_fav_row(), source_system="ultoim_v2") > 0
    assert tg.sent == []  # accepted but NOTIFY off -> silent shadow

    monkeypatch.setenv("Q15_V3_FAV10M_NOTIFY", "true")
    assert runtime.record_source_row(
        _fav_row(ticker="KXETH-10M-2", window_key=78), source_system="ultoim_v2"
    ) > 0
    fav_texts = [t for t in tg.sent if "V3 FAVORITE 10M" in t]
    assert len(fav_texts) == 1
    assert "BUY YES @ 87c" in fav_texts[0]
    assert "forward test" in fav_texts[0]

    led = runtime.get_ledger()
    fav_rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_FAV_10M]
    assert [r["decision_status"] for r in fav_rows] == [ACCEPTED, ACCEPTED]
    assert fav_rows[1]["notification_status"] == "SENT"


def test_fav_10m_exempt_from_empirical_interval_guard(tmp_path, monkeypatch):
    # 10M is in the guard's default late-interval set; the favorite book must not
    # be downgraded to RESEARCH_ONLY by it.
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    monkeypatch.setenv("Q15_V3_EMPIRICAL_DELIVERY_GUARD", "true")

    runtime.record_source_row(_fav_row(), source_system="ultoim_v2")
    led = runtime.get_ledger()
    fav = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_FAV_10M]
    assert fav and fav[0]["decision_status"] == ACCEPTED


# -- message builders ---------------------------------------------------------------

def _warn_alert_row():
    return {
        "bot_name": BOT_WARN_FLIP,
        "asset": "BTC",
        "side": "NO",
        "ticker": "KXBTC-WF",
        "entry_ask_cents": 67.0,
        "reason_codes": "V3_WARN_FLIP_EVAL,BAND_OK,TIER_PRIME",
        "threshold_json": json.dumps({
            "tier": "PRIME",
            "band_lo_cents": 55.0,
            "band_hi_cents": 75.0,
            "chase_max_cents": 68.0,
            "warn_seconds_remaining": 305.0,
            "confirm_cycles": 6,
            "confirm_span_seconds": 21.0,
            "resolved_n": 14,
            "resolved_correct": 12,
            "resolved_accuracy": 0.857,
            "resolved_wilson_lb": 0.712,
            "ev_cents": 17.0,
            "ev_prior_source": "discovery_n58",
        }),
        "original_source_side": "YES",
    }


def test_warn_flip_alert_content_and_marker_safety():
    text = build_warn_flip_alert(_warn_alert_row())
    assert "V3 WARN-FLIP ENTRY" in text and "BTC NO" in text
    assert "BUY NO @ 67c" in text
    assert "chase ≤ 68c" in text
    assert "Tier: PRIME" in text
    assert "Confirmed flip: 6 cycles / 21s" in text
    assert "Book: 12W-2L" in text
    assert "EV ≈ 17c/contract after fees (prior)" in text
    assert "Flipped from: YES entry" in text
    # champion-path markers must never leak into V3 messages
    for marker in ("ENTRY RECOMMENDED", "NO ENTRY YET", "V9.5 CHECK", "Hourly Report"):
        assert marker not in text
    # dispatcher routes by bot_name
    assert build_v3_alert(_warn_alert_row()) == text


def test_fav_10m_alert_content_and_dispatch():
    row = {
        "bot_name": BOT_FAV_10M,
        "asset": "ETH",
        "side": "YES",
        "ticker": "KXETH-10M",
        "entry_ask_cents": 87.0,
        "reason_codes": "V3_FAV10M_EVAL,BAND_OK,SPREAD_OK",
        "threshold_json": json.dumps({
            "band_lo_cents": 85.0,
            "band_hi_cents": 90.0,
            "resolved_n": 0,
            "ev_cents": 3.0,
            "ev_prior_source": "backtest_n656",
        }),
    }
    text = build_fav_10m_alert(row)
    assert "V3 FAVORITE 10M" in text and "ETH YES" in text
    assert "BUY YES @ 87c" in text
    assert "chase ≤ 88c" in text
    assert "Book: no resolved trades yet" in text
    assert "EV ≈ 3c/contract after fees (backtest prior)" in text
    assert "forward test" in text
    assert build_v3_alert(row) == text


# -- feeds -------------------------------------------------------------------------

class Fav10mFeedTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from q15_upgrade.interval_research.runner import IntervalResearchRunner
        from tests.test_interval_research import _cfg

        self.tmp = tempfile.mkdtemp()
        self.r = IntervalResearchRunner(_cfg(self.tmp))

    def test_10m_capture_feeds_v3_when_flag_on(self):
        from tests.test_interval_research import _Canon, _analysis

        analysis = _analysis(side="YES", ask=87.0, edge=2.0)
        with patch.dict(os.environ, {"Q15_V3_FAV10M_FEED": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_source_row") as record:
                self.r.observe(
                    analyses={"ETH": analysis},
                    canonicals={"ETH": _Canon("KX10", 600, settlement_time=9000.0)},
                    now=1000.0,
                )
        record.assert_called_once()
        source_row = record.call_args.args[0]
        self.assertEqual(record.call_args.kwargs["source_system"], "ultoim_v2")
        self.assertEqual(source_row["interval"], "10M")
        self.assertEqual(source_row["record_kind"], "INTERVAL_RESEARCH_10M")
        self.assertEqual(source_row["entry_ask_cents"], 87.0)

    def test_10m_capture_feed_default_off(self):
        from tests.test_interval_research import _Canon, _analysis

        os.environ.pop("Q15_V3_FAV10M_FEED", None)
        with patch("q15_upgrade.strategy_bots.runtime.record_source_row") as record:
            self.r.observe(
                analyses={"ETH": _analysis(side="YES", ask=87.0)},
                canonicals={"ETH": _Canon("KX10-OFF", 600, settlement_time=9000.0)},
                now=1000.0,
            )
        record.assert_not_called()


def test_fire_exit_warning_feeds_book1(tmp_path, monkeypatch):
    """The ultoim_v2 warning path forwards a warn-flip source row to V3."""
    from tests.test_ultoim_v2 import _StubTelegram, _ew_cfg_over, _row, _runner
    from q15_upgrade.ultoim_v2.runner import _window_key

    tg = _StubTelegram()
    r = _runner(tmp_path, telegram=tg, **_ew_cfg_over())
    window_key = _window_key(9000.0, 1000.0)
    r.ledger.record_decision(_row(ticker="T-BTC", window_key=window_key, close_time=9000.0))
    entry = r.ledger.find_fired_entry("ultoim-v2", "T-BTC", window_key)
    cand = {
        "asset": "BTC", "ticker": "T-BTC", "predicted_side": "YES",
        "selected_probability": 0.66, "calibrated_yes_probability": 0.66,
        "market_implied_yes_probability": 0.66, "seconds_remaining": 390.0,
        "close_time": 9000.0, "entry_ask_cents": 67.0, "spread_cents": 2.0,
        "flip_probability": 18.0, "manipulation_suspected": False,
    }
    with patch("q15_upgrade.strategy_bots.runtime.record_exit_warning_row") as record:
        r._fire_exit_warning(cand, entry, window_key, 1215.0,
                             {"first_seen": 1200.0, "count": 2.0})
    record.assert_called_once()
    source_row = record.call_args.args[0]
    assert source_row["record_kind"] == "EXIT_WARNING_FLIP"
    assert source_row["predicted_side"] == "YES"
    assert source_row["entry_ask_cents"] == 67.0
    assert source_row["warn_seconds_remaining"] == 390.0
    assert source_row["confirm_cycles"] == 2
    assert source_row["model_version"] == "ultoim-v2"
