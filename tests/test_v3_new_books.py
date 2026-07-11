"""Tests for the two new V3 books.

Book 1 (warn_flip_entry): follow a confirmed ultoim_v2 exit-warning flip when the
flip side's live ask is inside the pre-registered 55-75c band.
Book 2 (fav_10m): buy the predicted (favorite) side at the 10M mark inside the
85-90c band; shadow-first (NOTIFY behind its own flag).

Both are paper-only and self-governed (empirical Wilson-LB EV once n>=30,
auto-mute governor). Since 2026-07-05 the books and their NOTIFY flags default
ON (owner directive); tests that exercise the disabled paths set the env flags
to false explicitly.
"""
from __future__ import annotations

import json
import os
import sqlite3
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

def test_warn_flip_default_on_and_explicit_off(monkeypatch):
    os.environ.pop("Q15_V3_WARN_FLIP", None)  # default is ON per owner directive
    assert warn_flip_entry_decision(_warn_row(), source_system="ultoim_v2") is not None
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "false")
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
    monkeypatch.setenv("Q15_V3_FAV10M", "false")
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


def test_decisions_for_row_includes_fav_10m_by_default(monkeypatch):
    row = _fav_row()
    os.environ.pop("Q15_V3_FAV10M", None)  # default ON
    bots = {d.bot_name for d in decisions_for_row(row, source_system="ultoim_v2")}
    assert BOT_FAV_10M in bots
    monkeypatch.setenv("Q15_V3_FAV10M", "false")
    bots = {d.bot_name for d in decisions_for_row(row, source_system="ultoim_v2")}
    assert BOT_FAV_10M not in bots


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
    monkeypatch.setenv("Q15_V3_WARN_FLIP_NOTIFY", "false")

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
    monkeypatch.setenv("Q15_V3_WARN_FLIP", "false")
    assert runtime.record_exit_warning_row(_warn_row()) is None
    led = runtime.get_ledger()
    assert [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_WARN_FLIP] == []


def test_fav_10m_notify_gating_and_message(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_FAV10M", "true")
    monkeypatch.setenv("Q15_V3_FAV10M_NOTIFY", "false")

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

    def test_10m_capture_feed_default_on_explicit_off(self):
        from tests.test_interval_research import _Canon, _analysis

        os.environ.pop("Q15_V3_FAV10M_FEED", None)  # default ON now
        with patch("q15_upgrade.strategy_bots.runtime.record_source_row") as record:
            self.r.observe(
                analyses={"ETH": _analysis(side="YES", ask=87.0)},
                canonicals={"ETH": _Canon("KX10-DEF", 600, settlement_time=9000.0)},
                now=1000.0,
            )
        record.assert_called_once()
        with patch.dict(os.environ, {"Q15_V3_FAV10M_FEED": "false"}):
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


# -- Top Pick 13M (display-only book) -----------------------------------------------

def _top_pick_row(**over):
    base = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "SOL",
        "ticker": "KXSOL-13M",
        "interval": "13M",
        "window_key": 55,
        "close_time": 1780.0,
        "record_kind": "TOP_PICK_13M",
        "delivery_status": "RECORDED",
        "predicted_side": "NO",
        "calibrated_yes_probability": 0.29,
        "entry_ask_cents": 68.0,
        "spread_cents": 2.0,
        "top_pick_slate_n": 7,
        "top_pick_extremity": 18.0,
        "top_pick_runner_up_asset": "ETH",
        "top_pick_runner_up_extremity": 12.0,
    }
    base.update(over)
    return base


def test_top_pick_decision_gates(monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_TOP_PICK_13M, top_pick_13m_decision

    monkeypatch.setenv("Q15_V3_TOP_PICK_13M", "false")
    assert top_pick_13m_decision(_top_pick_row()) is None
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M", "true")
    assert top_pick_13m_decision(_top_pick_row(record_kind="DELIVERED_CANDIDATE")) is None
    d = top_pick_13m_decision(_top_pick_row())
    assert d is not None and d.bot_name == BOT_TOP_PICK_13M and d.decision_status == ACCEPTED
    assert d.threshold_profile["not_a_trade_signal"] is True
    assert d.threshold_profile["slate_n"] == 7


def test_record_top_pick_row_once_per_window_and_notify(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_TOP_PICK_13M

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M", "true")
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M_NOTIFY", "true")

    row_id = runtime.record_top_pick_row(_top_pick_row())
    assert row_id is not None
    # same window again (restart / concurrent) -> durable claim blocks a duplicate
    assert runtime.record_top_pick_row(_top_pick_row(ticker="KXSOL-13M-DUP")) is None

    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_TOP_PICK_13M]
    assert len(rows) == 1 and rows[0]["decision_status"] == ACCEPTED
    assert rows[0]["notification_status"] == "SENT"
    assert len(tg.sent) == 1
    text = tg.sent[0]
    assert "V3 BEST TRADE 13M" in text and "SOL NO" in text
    assert "BUY NO @ 68c" in text and "chase ≤ 69c" in text
    assert "model 71%" in text
    assert "Best of 7 this cycle" in text
    assert "size SMALL" in text
    for marker in ("ENTRY RECOMMENDED", "NO ENTRY YET", "V9.5 CHECK", "Hourly Report"):
        assert marker not in text


def test_record_top_pick_notify_explicit_off(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_TOP_PICK_13M

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M", "true")
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M_NOTIFY", "false")

    assert runtime.record_top_pick_row(_top_pick_row(window_key=56)) is not None
    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_TOP_PICK_13M]
    assert len(rows) == 1 and tg.sent == []


def test_record_top_pick_row_supports_legacy_meta_schema(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_TOP_PICK_13M

    db_path = tmp_path / "v3.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE strategy_bot_meta (key TEXT PRIMARY KEY, value TEXT, updated_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M", "true")
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M_NOTIFY", "true")

    row_id = runtime.record_top_pick_row(_top_pick_row(window_key=57))
    assert row_id is not None
    assert runtime.record_top_pick_row(_top_pick_row(window_key=57, ticker="KXSOL-LEGACY-DUP")) is None

    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_TOP_PICK_13M]
    assert len(rows) == 1
    assert rows[0]["notification_status"] == "SENT"
    assert len(tg.sent) == 1


class TopPick13mRunnerTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from q15_upgrade.interval_research.runner import IntervalResearchRunner
        from tests.test_interval_research import _cfg

        self.tmp = tempfile.mkdtemp()
        self.r = IntervalResearchRunner(_cfg(self.tmp))

    def _capture_slate(self):
        from tests.test_interval_research import _Canon, _analysis

        # three assets captured at the 13M mark (sr 780, band [755, 780]).
        # yes_ask_cents (the ranking input) comes from quote.ask_cents.
        analyses = {
            "BTC": _analysis(side="YES", ask=62.0),
            "ETH": _analysis(side="NO", ask=71.0),
            "SOL": _analysis(side="NO", ask=88.0),   # most extreme -> the pick
        }
        for name, quote_ask in (("BTC", 62.0), ("ETH", 71.0), ("SOL", 88.0)):
            analyses[name]["quote"]["ask_cents"] = quote_ask
        canonicals = {
            a: _Canon(f"KX{a}", 780, settlement_time=9000.0) for a in analyses
        }
        self.r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)
        return analyses

    def test_fires_once_with_most_extreme_pick(self):
        analyses = self._capture_slate()
        from tests.test_interval_research import _Canon

        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                # next cycle: sr has ticked into the firing band [740, 770]
                canonicals = {a: _Canon(f"KX{a}", 760, settlement_time=9000.0) for a in analyses}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1020.0)
                # further cycles in-band must not fire again
                canonicals = {a: _Canon(f"KX{a}", 750, settlement_time=9000.0) for a in analyses}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1030.0)

        record.assert_called_once()
        source_row = record.call_args.args[0]
        self.assertEqual(source_row["record_kind"], "TOP_PICK_13M")
        self.assertEqual(source_row["asset"], "SOL")          # fav band beats all
        self.assertEqual(source_row["predicted_side"], "NO")
        self.assertEqual(source_row["top_pick_slate_n"], 3)
        # profit ranking: SOL (+0.35 fav band) > BTC (60-70: -0.98) > ETH (70-80: -1.05)
        self.assertEqual(source_row["top_pick_runner_up_asset"], "BTC")
        self.assertTrue(source_row["top_pick_fav_band"])
        self.assertAlmostEqual(source_row["top_pick_bucket_ev_cents"], 0.35)

    def test_flag_off_never_fires(self):
        analyses = self._capture_slate()
        from tests.test_interval_research import _Canon

        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                canonicals = {a: _Canon(f"KX{a}", 760, settlement_time=9000.0) for a in analyses}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1020.0)
        record.assert_not_called()

    def test_min_assets_gate(self):
        from tests.test_interval_research import _Canon, _analysis

        analyses = {"BTC": _analysis(side="YES", ask=62.0)}
        canonicals = {"BTC": _Canon("KXBTC", 780, settlement_time=9000.0)}
        self.r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                canonicals = {"BTC": _Canon("KXBTC", 760, settlement_time=9000.0)}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1020.0)
        record.assert_not_called()


# -- v3.1: pick grades (ranking untouched, card labeled by measured cell) -----------

def test_v31_pick_grade_matrix():
    from q15_upgrade.interval_research.runner import IntervalResearchRunner as R

    assert R._pick_grade("BTC", 87.0) == ("SKIP", "MAJOR_EFFICIENT_BOOK")
    assert R._pick_grade("ETH", 65.0) == ("SKIP", "MAJOR_EFFICIENT_BOOK")
    assert R._pick_grade("SOL", 87.0) == ("TRADE", "ALT_FAVORITE_BAND")
    assert R._pick_grade("DOGE", 65.0) == ("CAUTION", "ALT_FALLBACK_BAND")
    assert R._pick_grade("XRP", 92.0) == ("SKIP", "OUT_OF_MEASURED_BANDS")
    assert R._pick_grade("BNB", 55.0) == ("SKIP", "OUT_OF_MEASURED_BANDS")


def test_v31_skip_assets_env_override(monkeypatch):
    from q15_upgrade.interval_research.runner import IntervalResearchRunner as R

    monkeypatch.setenv("Q15_V3_TOP_PICK_SKIP_ASSETS", "BTC")
    assert R._pick_grade("ETH", 87.0) == ("TRADE", "ALT_FAVORITE_BAND")
    assert R._pick_grade("BTC", 87.0) == ("SKIP", "MAJOR_EFFICIENT_BOOK")


def test_v31_runner_stamps_grade_on_source_row():
    import tempfile

    from q15_upgrade.interval_research.runner import IntervalResearchRunner
    from tests.test_interval_research import _Canon, _analysis, _cfg

    tmp = tempfile.mkdtemp()
    r = IntervalResearchRunner(_cfg(tmp))
    analyses = {
        "BTC": _analysis(side="YES", ask=62.0),
        "ETH": _analysis(side="NO", ask=71.0),
        "SOL": _analysis(side="NO", ask=88.0),
    }
    for name, quote_ask in (("BTC", 62.0), ("ETH", 71.0), ("SOL", 88.0)):
        analyses[name]["quote"]["ask_cents"] = quote_ask
    canonicals = {a: _Canon(f"KX{a}", 780, settlement_time=9000.0) for a in analyses}
    r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)
    with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
        with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
            canonicals = {a: _Canon(f"KX{a}", 760, settlement_time=9000.0) for a in analyses}
            r.observe(analyses=analyses, canonicals=canonicals, now=1020.0)
    row = record.call_args.args[0]
    assert row["asset"] == "SOL"
    assert row["top_pick_grade"] == "TRADE"
    assert row["top_pick_grade_reason"] == "ALT_FAVORITE_BAND"


def test_v31_card_shows_grades():
    base = {
        "bot_name": "top_pick_13m",
        "asset": "SOL", "side": "NO", "ticker": "KXSOL-13M", "entry_ask_cents": 87.0,
    }
    trade = dict(base, threshold_json=json.dumps({"grade": "TRADE", "slate_n": 7}))
    text = build_v3_alert(trade)
    assert "Grade: ✅ TRADE" in text
    caution = dict(base, entry_ask_cents=66.0,
                   threshold_json=json.dumps({"grade": "CAUTION", "slate_n": 7}))
    assert "Grade: ⚠️ CAUTION" in build_v3_alert(caution)
    skip = dict(base, asset="BTC",
                threshold_json=json.dumps({"grade": "SKIP", "grade_reason": "MAJOR_EFFICIENT_BOOK"}))
    stext = build_v3_alert(skip)
    assert "Grade: ⛔ SKIP" in stext and "do not trade" in stext
    for marker in ("ENTRY RECOMMENDED", "NO ENTRY YET", "V9.5 CHECK", "Hourly Report"):
        assert marker not in stext


# -- hard requirement: exactly one pick per 15m window --------------------------------

class OnePickPerWindowTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        from q15_upgrade.interval_research.runner import IntervalResearchRunner
        from tests.test_interval_research import _cfg

        self.tmp = tempfile.mkdtemp()
        self.r = IntervalResearchRunner(_cfg(self.tmp))

    def _one_asset(self, sr, now, ticker="KXSOL"):
        from tests.test_interval_research import _Canon, _analysis

        a = _analysis(side="NO", ask=88.0)
        a["quote"]["ask_cents"] = 88.0
        self.r.observe(analyses={"SOL": a},
                       canonicals={"SOL": _Canon(ticker, sr, settlement_time=9000.0)}, now=now)

    def test_fallback_fires_with_thin_slate(self):
        # only ONE asset scored at 13M: primary window (min_assets=3) never fires...
        self._one_asset(780, 1000.0)
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                self._one_asset(760, 1020.0)   # primary band, slate too thin -> no fire
                record.assert_not_called()
                self._one_asset(700, 1080.0)   # fallback band -> fires with slate of 1
        record.assert_called_once()
        row = record.call_args.args[0]
        self.assertEqual(row["asset"], "SOL")
        self.assertEqual(row["top_pick_phase"], "FALLBACK")
        self.assertEqual(row["top_pick_slate_n"], 1)
        self.assertIsNone(row["top_pick_runner_up_asset"])

    def test_fallback_never_double_fires_after_primary(self):
        from tests.test_interval_research import _Canon, _analysis

        analyses = {}
        for name, ask in (("BTC", 62.0), ("ETH", 71.0), ("SOL", 88.0)):
            a = _analysis(side="NO", ask=ask); a["quote"]["ask_cents"] = ask
            analyses[name] = a
        canonicals = {n: _Canon(f"KX{n}", 780, settlement_time=9000.0) for n in analyses}
        self.r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                canonicals = {n: _Canon(f"KX{n}", 760, settlement_time=9000.0) for n in analyses}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1020.0)  # primary
                canonicals = {n: _Canon(f"KX{n}", 700, settlement_time=9000.0) for n in analyses}
                self.r.observe(analyses=analyses, canonicals=canonicals, now=1080.0)  # fallback band
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0]["top_pick_phase"], "PRIMARY")

    def test_gap_notice_when_nothing_scorable(self):
        from tests.test_interval_research import _Canon, _analysis

        # capture never lands (model can't score) -> zero slate all the way down
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_top_pick_row") as record:
                with patch("q15_upgrade.strategy_bots.runtime.send_top_pick_gap_notice") as gap:
                    self.r.observe(analyses={"SOL": _analysis(available=False)},
                                   canonicals={"SOL": _Canon("KXGAP", 610, settlement_time=9000.0)},
                                   now=1000.0)
        record.assert_not_called()
        gap.assert_called_once()
        self.assertEqual(gap.call_args.kwargs["window_key"], 10)


def test_gap_notice_runtime_sends_once(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_TOP_PICK_13M_NOTIFY", "true")
    assert runtime.send_top_pick_gap_notice(window_key=99) is True
    assert runtime.send_top_pick_gap_notice(window_key=99) is False  # durable claim
    assert len(tg.sent) == 1
    assert "NO PICK" in tg.sent[0] and "data gap" in tg.sent[0]


def test_fallback_card_annotation():
    row = {
        "bot_name": "top_pick_13m",
        "asset": "SOL", "side": "NO", "ticker": "KXSOL-13M", "entry_ask_cents": 66.0,
        "threshold_json": json.dumps({"grade": "CAUTION", "slate_n": 1, "pick_phase": "FALLBACK"}),
    }
    text = build_v3_alert(row)
    assert "late/thin slate (fallback fire)" in text


# -- drift pick 13M (Drift Shadow base-book card; owner-approved 2026-07-08) --

def _drift_row(**over):
    base = {
        "created_at": 5000.0,
        "model_version": "v95",
        "record_kind": "DRIFT_PICK_13M",
        "asset": "XRP",
        "ticker": "KXXRP15M-DP",
        "interval": "13M",
        "window_key": 777,
        "close_time": 4_102_444_800.0,
        "predicted_side": "YES",
        "entry_ask_cents": 60.0,
        "spread_cents": 3.0,
        "depth_contracts": 250.0,
        "drift_spread_weight": 1.5,
        "drift_session_weight": 1.33,
        "drift_stack_weight": 1.5,
        "drift_disagreement": -0.05,
        "drift_pick_rank": 1,
        "drift_slate_n": 2,
        "drift_book_n_resolved": 11,
        "drift_book_wins": 8,
        "drift_book_total_pnl_cents": 72.0,
        "drift_book_status": "ACCRUING",
        "drift_book_verdict_n": 60,
    }
    base.update(over)
    return base


def _drift_checkpoint_row(**over):
    base = {
        "created_at": 1100.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_ADDON_REQUAL",
        "delivery_status": "PAPER_ADDON",
        "asset": "XRP",
        "ticker": "KXXRP15M-ADD",
        "interval": "12M",
        "window_key": 778,
        "close_time": 4_102_444_800.0,
        "predicted_side": "YES",
        "entry_ask_cents": 66.0,
        "spread_cents": 2.0,
        "depth_contracts": 80.0,
        "drift_base_ask_cents": 64.0,
        "drift_ask13_cents": None,
        "drift_disagreement": -0.08,
        "drift_rule_version": "drift-addon-requal-12m-7m-v1",
        "drift_track_n_resolved": 13,
        "drift_track_wins": 10,
        "drift_track_total_pnl_cents": 113.0,
        "drift_track_status": "ACCRUING",
        "drift_track_verdict_n": 40,
    }
    base.update(over)
    return base


def test_drift_decision_gates(monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_DRIFT_13M, drift_pick_13m_decision

    monkeypatch.setenv("Q15_V3_DRIFT_13M", "false")
    assert drift_pick_13m_decision(_drift_row()) is None
    monkeypatch.setenv("Q15_V3_DRIFT_13M", "true")
    assert drift_pick_13m_decision(_drift_row(record_kind="TOP_PICK_13M")) is None
    assert drift_pick_13m_decision(_drift_row(predicted_side="NO")) is None
    assert drift_pick_13m_decision(_drift_row(entry_ask_cents=74.0)) is None
    d = drift_pick_13m_decision(_drift_row())
    assert d is not None and d.bot_name == BOT_DRIFT_13M and d.decision_status == ACCEPTED
    assert d.threshold_profile["stack_weight"] == 1.5
    assert d.threshold_profile["book_n_resolved"] == 11


def test_record_drift_pick_dedup_per_window_ticker_and_card(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_DRIFT_FLOW_SPREAD

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_DRIFT_13M", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "true")
    monkeypatch.setattr(
        runtime,
        "enrich_spot_depth",
        lambda row: dict(
            row,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=500.0,
        ),
    )
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))

    assert runtime.record_drift_pick_row(_drift_row()) is not None
    # same (window, ticker) -> durable claim blocks; same window, NEW ticker -> allowed
    assert runtime.record_drift_pick_row(_drift_row()) is None
    assert runtime.record_drift_pick_row(_drift_row(ticker="KXDOGE15M-DP")) is not None

    led = runtime.get_ledger()
    rows = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_DRIFT_FLOW_SPREAD]
    assert len(rows) == 2 and all(r["decision_status"] == ACCEPTED for r in rows)
    assert len(tg.sent) == 2
    text = tg.sent[0]
    assert "DRIFT FLOW CONFIRMED 13M" in text and "FULL SIZE" in text
    assert "Confirmed: 60s spot flow" in text
    assert "BUY YES — XRP @ 60¢" in text and "breakeven 62%" in text
    assert "rest at 60¢" in text
    assert "no resolved picks yet" in text and "verdict at n=60" in text
    assert "<pre>" in text  # v2-channel panel grammar: header outside, body inside
    for marker in ("ENTRY RECOMMENDED", "NO ENTRY YET", "V9.5 CHECK",
                   "Hourly Report", "TOP 3 PICKS"):
        assert marker not in text


def test_drift_card_thin_depth_half_size_and_empty_book():
    from q15_upgrade.strategy_bots.telegram import build_drift_pick_alert

    row = {
        "bot_name": "drift_13m", "asset": "DOGE", "ticker": "T",
        "entry_ask_cents": 67.0,
        "threshold_json": json.dumps({
            "stack_weight": 0.5, "spread_cents": 5.0, "session_weight": 0.75,
            "depth_contracts": 12.0, "book_n_resolved": 0, "book_verdict_n": 60}),
    }
    text = build_drift_pick_alert(row)
    assert "HALF SIZE" in text
    assert "downsize" in text
    assert "pay 68¢ now" in text           # thin book -> immediate +1c chase
    assert "no resolved picks yet" in text
    assert "breakeven 69%" in text         # 67c + 2c fee


def test_drift_notify_explicit_off(tmp_path, monkeypatch):
    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "false")
    monkeypatch.setattr(
        runtime,
        "enrich_spot_depth",
        lambda row: dict(
            row,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=500.0,
        ),
    )
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))
    assert runtime.record_drift_pick_row(_drift_row(window_key=778)) is not None
    assert tg.sent == []


def test_drift_checkpoint_decisions_and_10m_latequal_exclusion(monkeypatch):
    from q15_upgrade.strategy_bots.rules import (
        BOT_DRIFT_ADDON,
        BOT_DRIFT_LATEQUAL,
        RESEARCH_ONLY,
        drift_addon_requal_decision,
        drift_latequal_decision,
    )

    monkeypatch.setenv("Q15_V3_DRIFT_ADDON", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_LATEQUAL", "true")
    addon = drift_addon_requal_decision(_drift_checkpoint_row())
    assert addon is not None and addon.bot_name == BOT_DRIFT_ADDON
    assert addon.decision_status == ACCEPTED
    assert addon.threshold_profile["counts_as_independent_pick"] is False
    assert addon.threshold_profile["max_addon_weight"] == 0.5

    late_row = _drift_checkpoint_row(
        record_kind="DRIFT_LATEQUAL",
        delivery_status="RESEARCH",
        ticker="KXXRP15M-LATE",
        drift_base_ask_cents=None,
        drift_ask13_cents=55.0,
        drift_rule_version="drift-latequal-12m-11m-v1",
    )
    late = drift_latequal_decision(late_row)
    assert late is not None and late.bot_name == BOT_DRIFT_LATEQUAL
    assert late.decision_status == RESEARCH_ONLY
    assert late.threshold_profile["counts_as_independent_pick"] is True
    assert drift_latequal_decision(dict(late_row, interval="10M")) is None


def test_drift_checkpoint_cards_accounting_and_settlement(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_DRIFT_ADDON, BOT_DRIFT_LATEQUAL

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_DRIFT_ADDON", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_ADDON_NOTIFY", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_LATEQUAL", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_LATEQUAL_NOTIFY", "true")
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))

    addon_row = _drift_checkpoint_row()
    late_row = _drift_checkpoint_row(
        record_kind="DRIFT_LATEQUAL",
        delivery_status="RESEARCH",
        ticker="KXXRP15M-LATE",
        drift_base_ask_cents=None,
        drift_ask13_cents=55.0,
        drift_rule_version="drift-latequal-12m-11m-v1",
    )
    assert runtime.record_drift_checkpoint_row(addon_row) is not None
    assert runtime.record_drift_checkpoint_row(addon_row) is None
    assert runtime.record_drift_checkpoint_row(late_row) is not None
    assert len(tg.sent) == 2
    assert "DRIFT ADD-ON 12M" in tg.sent[0]
    assert "NOT an independent accuracy sample" in tg.sent[0]
    assert "DRIFT LATE QUAL 12M" in tg.sent[1]
    assert "RESEARCH ONLY" in tg.sent[1]

    assert runtime.reconcile_drift_settlements([
        {"model_version": "interval-research-v1", "ticker": addon_row["ticker"],
         "official_result": "YES", "resolved_at": 1900.0},
        {"model_version": "interval-research-v1", "ticker": late_row["ticker"],
         "official_result": "NO", "resolved_at": 1900.0},
    ]) == 2
    ledger = runtime.get_ledger()
    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)
    assert scoreboard["all"]["rows"] == 1
    assert scoreboard["all_exposure"]["rows"] == 2
    # The late qualifier is RESEARCH_ONLY: retain it in the diagnostic funnel,
    # but never count it as a deployable independent pick.
    assert scoreboard["drift_system"]["independent_picks"]["rows"] == 0
    assert scoreboard["drift_system"]["independent_candidates"]["rows"] == 1
    assert scoreboard["drift_system"]["correlated_addon_exposure"]["rows"] == 1
    assert scoreboard["by_bot"][BOT_DRIFT_ADDON]["resolved"] == 1
    assert scoreboard["by_bot"][BOT_DRIFT_LATEQUAL]["resolved"] == 1


def test_drift_checkpoint_runner_adapter_forwards_new_rows():
    from types import SimpleNamespace
    from q15_upgrade.interval_research.runner import IntervalResearchRunner

    runner = object.__new__(IntervalResearchRunner)
    runner.config = SimpleNamespace(model_version="interval-research-v1")

    class Recorder:
        def checkpoint_rows_recorded_at(self, model_version, window_key, interval, now):
            assert (model_version, window_key, interval, now) == (
                "interval-research-v1", 44, "11M", 1200.0)
            return [{
                "record_kind": "DRIFT_LATEQUAL",
                "created_at": 1200.0,
                "asset": "SOL",
                "ticker": "KXSOL-LATE",
                "close_time": 1800.0,
                "ask_cents": 64.0,
                "ask13_cents": 56.0,
                "spread_cents": 2.0,
                "depth_contracts": 90.0,
                "disagreement": -0.08,
            }]

        def scoreboard(self):
            return {"book_latequal": {
                "n_resolved": 8,
                "win_rate": 0.75,
                "total_pnl_cents": 62.0,
                "status": "ACCRUING",
            }}

    with patch("q15_upgrade.strategy_bots.runtime.record_drift_checkpoint_row") as record:
        runner._alert_drift_checkpoint_rows(Recorder(), 44, "11M", 1200.0)
    record.assert_called_once()
    row = record.call_args.args[0]
    assert row["record_kind"] == "DRIFT_LATEQUAL"
    assert row["drift_rule_version"] == "drift-latequal-12m-11m-v1"
    assert row["drift_independent_pick"] is True
    assert row["drift_track_wins"] == 6


# -- Drift NO mirror: filtered positive cohorts, one grouped research card ----

def _drift_no_row(**over):
    base = {
        "created_at": 2000.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_NO_MIRROR",
        "rule_code": "DRIFT_NO_MIRROR_POSITIVE_FILTER_V1",
        "reason_codes": "DRIFT_NO_MIRROR_RESEARCH,MID_PRICE_65_69,TIGHT_SPREAD,BTC_AGREES_NO",
        "drift_no_tags": "DRIFT_NO_MIRROR_RESEARCH,MID_PRICE_65_69,TIGHT_SPREAD,BTC_AGREES_NO",
        "delivery_status": "RESEARCH",
        "asset": "XRP",
        "ticker": "KXXRP15M-NO",
        "interval": "13M",
        "window_key": 1200,
        "close_time": 4_102_444_800.0,
        "predicted_side": "NO",
        "entry_ask_cents": 67.0,
        "spread_cents": 2.0,
        "depth_contracts": 100.0,
        "drift_btc_side_at_capture": "NO",
        "drift_track_n_resolved": 0,
        "drift_track_verdict_n": 60,
    }
    base.update(over)
    return base


def test_drift_no_mirror_decision_excludes_negative_and_untagged(monkeypatch):
    from q15_upgrade.strategy_bots.rules import (
        BOT_DRIFT_NO_MIRROR,
        RESEARCH_ONLY,
        drift_no_mirror_decision,
    )

    monkeypatch.setenv("Q15_V3_DRIFT_NO_MIRROR", "true")
    decision = drift_no_mirror_decision(_drift_no_row())
    assert decision is not None and decision.bot_name == BOT_DRIFT_NO_MIRROR
    assert decision.decision_status == RESEARCH_ONLY and decision.side_override == "NO"
    assert decision.entry_ask_cents == 67.0
    assert decision.threshold_profile["excluded_negative_assets"] == ["BNB", "DOGE"]

    assert drift_no_mirror_decision(_drift_no_row(asset="BNB")) is None
    assert drift_no_mirror_decision(_drift_no_row(asset="DOGE")) is None
    assert drift_no_mirror_decision(_drift_no_row(asset="ETH")) is None
    assert drift_no_mirror_decision(_drift_no_row(predicted_side="YES")) is None
    assert drift_no_mirror_decision(_drift_no_row(entry_ask_cents=74.0)) is None
    assert drift_no_mirror_decision(_drift_no_row(
        reason_codes="DRIFT_NO_MIRROR_RESEARCH,TIGHT_SPREAD",
        drift_no_tags="DRIFT_NO_MIRROR_RESEARCH,TIGHT_SPREAD",
    )) is None


def test_drift_no_mirror_runtime_groups_and_separates_scoreboard(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.rules import BOT_DRIFT_NO_MIRROR

    tg = _Telegram()
    _reset_runtime(tmp_path, monkeypatch, tg)
    monkeypatch.setenv("Q15_V3_DRIFT_NO_MIRROR", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_MIRROR_NOTIFY", "true")
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))

    rows = [
        _drift_no_row(),
        _drift_no_row(
            asset="HYPE",
            ticker="KXHYPE15M-NO",
            entry_ask_cents=66.0,
            spread_cents=5.0,
            reason_codes="DRIFT_NO_MIRROR_RESEARCH,MID_PRICE_65_69",
            drift_no_tags="DRIFT_NO_MIRROR_RESEARCH,MID_PRICE_65_69",
            drift_btc_side_at_capture="YES",
        ),
        _drift_no_row(asset="BNB", ticker="KXBNB-EXCLUDED"),
    ]
    row_ids = runtime.record_drift_no_mirror_window(rows)
    assert len(row_ids) == 2
    assert runtime.record_drift_no_mirror_window(rows) == []
    assert len(tg.sent) == 1
    text = tg.sent[0]
    assert "DRIFT NO WATCH \u2014 RESEARCH ONLY" in text
    assert "XRP NO @ 67c" in text and "HYPE NO @ 66c" in text
    assert "BNB" in text  # exclusion disclosure only
    assert "KXBNB-EXCLUDED" not in text
    assert "no order is placed" in text

    ledger = runtime.get_ledger()
    recorded = [
        row for row in ledger.rows(STRATEGY_VERSION)
        if row["bot_name"] == BOT_DRIFT_NO_MIRROR
    ]
    assert len(recorded) == 2
    assert all(row["decision_status"] == "RESEARCH_ONLY" for row in recorded)
    assert all(row["side"] == "NO" for row in recorded)
    assert {row["notification_status"] for row in recorded} == {"SENT"}
    assert len({row["notification_message_id"] for row in recorded}) == 1

    assert runtime.reconcile_drift_settlements([
        {"model_version": "interval-research-v1", "ticker": "KXXRP15M-NO",
         "official_result": "NO", "resolved_at": 2800.0},
        {"model_version": "interval-research-v1", "ticker": "KXHYPE15M-NO",
         "official_result": "YES", "resolved_at": 2800.0},
    ]) == 2
    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)
    drift = scoreboard["drift_system"]
    assert drift["independent_picks"]["rows"] == 0  # YES book remains untouched
    assert drift["no_mirror_research"]["rows"] == 2
    assert drift["no_mirror_research"]["resolved"] == 2
    assert drift["no_mirror_by_asset"]["XRP"]["correct"] == 1
    assert drift["no_mirror_by_asset"]["HYPE"]["correct"] == 0


def test_drift_no_expansion_runner_adapter_groups_window():
    from types import SimpleNamespace
    from q15_upgrade.interval_research.runner import IntervalResearchRunner

    runner = object.__new__(IntervalResearchRunner)
    runner.config = SimpleNamespace(model_version="interval-research-v1")

    class Recorder:
        def no_mirror_rows_recorded_at(self, model_version, window_key, now):
            assert (model_version, window_key, now) == ("interval-research-v1", 55, 1000.0)
            return [{
                "created_at": 1000.0,
                "asset": "DOGE",
                "ticker": "KXDOGE-NO",
                "close_time": 1780.0,
                "ask_cents": 67.0,
                "spread_cents": 3.0,
                "depth_contracts": 70.0,
                "distance_sigma": 1e-5,
                "flip_probability": 20.0,
                "btc_side": "YES",
                "reason_codes": "DRIFT_NO_EXPANSION_CANDIDATE,DOGE_NO_65_69",
            }]

    with patch("q15_upgrade.strategy_bots.runtime.record_drift_no_expansion_window") as record:
        runner._alert_drift_no_expansion(Recorder(), 55, 1000.0)
    record.assert_called_once()
    payload = record.call_args.args[0]
    assert len(payload) == 1
    assert payload[0]["predicted_side"] == "NO"
    assert payload[0]["entry_ask_cents"] == 67.0
    assert payload[0]["record_kind"] == "DRIFT_NO_EXPANSION"
    assert payload[0]["distance_sigma"] == 1e-5
