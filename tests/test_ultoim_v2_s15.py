"""Tests for the Ultoim V2 15M selective-entry research SCREEN (fifteen_min.py).

The screen is RECORD-ONLY: it must (a) be scoped to 15M-NO candidates and inert
everywhere else, (b) compute the LUKEWARM/CHEAP core + CAL_DRIFT/FRESH tilt codes
correctly at their boundaries, (c) round-trip through the ledger and aggregate in the
research scoreboard, and (d) NEVER influence the live fire/delivery decision — a
confident NO still fires though it fails the screen, and a lukewarm NO still abstains
(per the existing gate) though it passes the screen.

Deterministic: no network, no worker threads (``_build_row`` is exercised directly).
"""
from __future__ import annotations

import pytest

from q15_upgrade.ultoim_v2 import fifteen_min, gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger
from q15_upgrade.ultoim_v2.runner import UltoimV2Runner


def _cfg(**over):
    return UltoimV2Config(enabled=True, model_version="ultoim-v2",
                          telegram_chat_id="", **over)


def _cand(**over):
    cand = {
        "predicted_side": "NO", "selected_probability": 0.54, "entry_ask_cents": 55.0,
        "calibrated_yes_probability": 0.40, "raw_yes_probability": 0.45,
        "seconds_remaining": 890.0, "total_cost_cents": 0.0,
    }
    cand.update(over)
    return cand


# --------------------------------------------------------------------------- #
# pure screen: scoping (15M-NO only)
# --------------------------------------------------------------------------- #
def test_inert_off_15m_and_off_no():
    cfg = _cfg()
    # 10M / 7M are never touched.
    for interval in ("10M", "7M"):
        f = fifteen_min.features(_cand(), interval, cfg)
        assert f == {"s15_pass": None, "s15_codes": None, "s15_cal_drift": None,
                     "s15_version": None}
    # YES side at 15M is never touched (delivery is NO-only; this is research scoping).
    f_yes = fifteen_min.features(_cand(predicted_side="YES"), "15M", cfg)
    assert f_yes["s15_pass"] is None and f_yes["s15_version"] is None
    assert fifteen_min.applies("15M", "NO") is True
    assert fifteen_min.applies("10M", "NO") is False
    assert fifteen_min.applies("15M", "YES") is False


# --------------------------------------------------------------------------- #
# pure screen: core + tilt codes and the pass verdict
# --------------------------------------------------------------------------- #
def test_core_pass_lukewarm_and_cheap():
    cfg = _cfg()
    v = fifteen_min.evaluate_15m(_cand(selected_probability=0.54, entry_ask_cents=55.0),
                                 "15M", cfg)
    assert v["applicable"] is True and v["passed"] is True
    assert "LUKEWARM" in v["codes"] and "CHEAP" in v["codes"]


def test_core_fails_when_confident():
    cfg = _cfg()
    v = fifteen_min.evaluate_15m(_cand(selected_probability=0.65, entry_ask_cents=55.0),
                                 "15M", cfg)
    assert v["passed"] is False and "LUKEWARM" not in v["codes"] and "CHEAP" in v["codes"]


def test_core_fails_when_expensive():
    cfg = _cfg()
    v = fifteen_min.evaluate_15m(_cand(selected_probability=0.54, entry_ask_cents=60.0),
                                 "15M", cfg)
    assert v["passed"] is False and "CHEAP" not in v["codes"]    # 60 is out of [47,60)


def test_tilt_codes_cal_drift_and_fresh():
    cfg = _cfg()
    # drift = 0.40 - 0.45 = -0.05 <= -0.03 -> CAL_DRIFT ; secs 890 >= 875 -> FRESH
    v = fifteen_min.evaluate_15m(_cand(), "15M", cfg)
    assert set(v["codes"]) == {"LUKEWARM", "CHEAP", "CAL_DRIFT", "FRESH"}
    assert v["cal_drift"] == pytest.approx(-0.05)
    # no drift / stale-quote -> tilts absent, core still passes
    v2 = fifteen_min.evaluate_15m(
        _cand(calibrated_yes_probability=0.44, raw_yes_probability=0.45,
              seconds_remaining=860.0), "15M", cfg)
    assert v2["passed"] is True
    assert "CAL_DRIFT" not in v2["codes"] and "FRESH" not in v2["codes"]


def test_boundaries_inclusive_exclusive():
    cfg = _cfg()  # sel_max 0.55, ask [47,60), cal_drift<=-0.03, fresh>=875
    # sel exactly 0.55 is NOT lukewarm (strict <)
    assert "LUKEWARM" not in fifteen_min.evaluate_15m(
        _cand(selected_probability=0.55), "15M", cfg)["codes"]
    # ask exactly 47 IS cheap (inclusive lo); exactly 60 is NOT (exclusive hi)
    assert "CHEAP" in fifteen_min.evaluate_15m(_cand(entry_ask_cents=47.0), "15M", cfg)["codes"]
    assert "CHEAP" not in fifteen_min.evaluate_15m(_cand(entry_ask_cents=60.0), "15M", cfg)["codes"]
    # cal_drift exactly -0.03 qualifies (inclusive)
    assert "CAL_DRIFT" in fifteen_min.evaluate_15m(
        _cand(calibrated_yes_probability=0.42, raw_yes_probability=0.45), "15M", cfg)["codes"]
    # fresh exactly 875 qualifies (inclusive); 874 does not
    assert "FRESH" in fifteen_min.evaluate_15m(_cand(seconds_remaining=875.0), "15M", cfg)["codes"]
    assert "FRESH" not in fifteen_min.evaluate_15m(_cand(seconds_remaining=874.0), "15M", cfg)["codes"]


def test_missing_data_short_circuits():
    cfg = _cfg()
    for bad in (_cand(selected_probability=None), _cand(entry_ask_cents=None),
                _cand(selected_probability=True), _cand(entry_ask_cents=False)):
        v = fifteen_min.evaluate_15m(bad, "15M", cfg)
        assert v["applicable"] is True and v["passed"] is False
        assert v["codes"] == ["MISSING_DATA"]


def test_cal_drift_none_when_inputs_missing():
    assert fifteen_min.cal_drift(None, 0.4) is None
    assert fifteen_min.cal_drift(0.4, None) is None
    assert fifteen_min.cal_drift(True, 0.4) is None           # bool rejected, not 1.0
    assert fifteen_min.cal_drift(0.30, 0.50) == pytest.approx(-0.20)


def test_features_maps_codes_to_columns():
    cfg = _cfg()
    f = fifteen_min.features(_cand(), "15M", cfg)
    assert f["s15_pass"] == 1
    assert f["s15_version"] == fifteen_min.S15_VERSION
    assert set(f["s15_codes"].split(",")) == {"LUKEWARM", "CHEAP", "CAL_DRIFT", "FRESH"}
    assert f["s15_cal_drift"] == pytest.approx(-0.05)


# --------------------------------------------------------------------------- #
# config defaults
# --------------------------------------------------------------------------- #
def test_config_defaults(monkeypatch):
    for name in ("Q15_ULTOIM_V2_S15_SEL_MAX", "Q15_ULTOIM_V2_S15_ASK_LO",
                 "Q15_ULTOIM_V2_S15_ASK_HI", "Q15_ULTOIM_V2_S15_CAL_DRIFT_MAX",
                 "Q15_ULTOIM_V2_S15_FRESH_MIN_SEC"):
        monkeypatch.delenv(name, raising=False)
    cfg = UltoimV2Config.from_env()
    assert cfg.s15_sel_max == 0.55 and cfg.s15_ask_lo == 47.0 and cfg.s15_ask_hi == 60.0
    assert cfg.s15_cal_drift_max == -0.03 and cfg.s15_fresh_min_seconds == 875.0


# --------------------------------------------------------------------------- #
# ledger: round-trip + research scoreboard
# --------------------------------------------------------------------------- #
def _row(**over):
    row = {
        "created_at": 1000.0, "model_version": "ultoim-v2", "asset": "BTC",
        "ticker": "T1", "interval": "15M", "window_key": 5, "mark_seconds": 900.0,
        "fired": 0, "predicted_side": "NO", "entry_ask_cents": 55.0,
        "delivery_status": "RECORDED",
        "s15_pass": 1, "s15_codes": "LUKEWARM,CHEAP,CAL_DRIFT,FRESH",
        "s15_cal_drift": -0.05, "s15_version": fifteen_min.S15_VERSION,
    }
    row.update(over)
    return row


def test_ledger_round_trip_and_scoreboard(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    # A: passes screen, settles NO -> win (+45). B: fails screen, settles YES -> loss (-58).
    led.record_decision(_row(ticker="A", entry_ask_cents=55.0, s15_pass=1))
    led.record_decision(_row(ticker="B", entry_ask_cents=58.0, s15_pass=0,
                             s15_codes="CHEAP", s15_cal_drift=0.0))
    led.resolve("ultoim-v2", "A", "NO", 9500.0)
    led.resolve("ultoim-v2", "B", "YES", 9500.0)

    # columns persisted
    rows = {r["ticker"]: r for r in led.recent_rows("ultoim-v2", limit=10)}
    assert rows["A"]["s15_pass"] == 1 and rows["A"]["s15_version"] == fifteen_min.S15_VERSION
    assert rows["A"]["s15_cal_drift"] == pytest.approx(-0.05)
    assert rows["B"]["s15_pass"] == 0

    sb = led.s15_research_scoreboard("ultoim-v2")
    assert sb["book"]["n"] == 2 and sb["book"]["pnl_total_cents"] == pytest.approx(-13.0)
    assert sb["gated"]["n"] == 1 and sb["gated"]["right"] == 1
    assert sb["gated"]["pnl_total_cents"] == pytest.approx(45.0)
    assert sb["gated_with_cal_drift"]["n"] == 1 and sb["gated_with_fresh"]["n"] == 1
    assert sb["version"] == fifteen_min.S15_VERSION
    led.close()


def test_scoreboard_excludes_other_marks_and_sides(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u.sqlite3"))
    led.record_decision(_row(ticker="N15", interval="15M", predicted_side="NO", s15_pass=1))
    led.record_decision(_row(ticker="N10", interval="10M", predicted_side="NO",
                             s15_pass=None, s15_codes=None, s15_version=None))
    led.record_decision(_row(ticker="Y15", interval="15M", predicted_side="YES",
                             s15_pass=None, s15_codes=None, s15_version=None))
    for t in ("N15", "N10", "Y15"):
        led.resolve("ultoim-v2", t, "NO", 9500.0)
    sb = led.s15_research_scoreboard("ultoim-v2")
    assert sb["book"]["n"] == 1                  # only the 15M-NO row counts
    assert sb["gated"]["n"] == 1
    led.close()


# --------------------------------------------------------------------------- #
# runner wiring: the screen NEVER gates the fire/delivery decision
# --------------------------------------------------------------------------- #
def _runner(tmp_path, **over):
    return UltoimV2Runner(_cfg(db_path=str(tmp_path / "u.sqlite3"),
                               min_confidence=0.55, ask_lo=50.0, ask_hi=72.0,
                               min_edge_cents=2.0, **over))


def test_runner_screen_does_not_block_a_fire(tmp_path):
    """A CONFIDENT 15M NO still fires (existing gate) even though it FAILS the screen
    (not lukewarm). s15 is recorded as a verdict, never as a veto."""
    r = _runner(tmp_path)
    cand = _cand(selected_probability=0.65, entry_ask_cents=58.0, total_cost_cents=0.0)
    verdict = gate.evaluate(cand, r.config)
    assert verdict["fired"] is True                       # gate fires on its own terms
    row = r._build_row(cand, verdict, "15M", 900, 5, 1000.0,
                       record_kind="DELIVERED_CANDIDATE", delivery_status="PENDING")
    assert row["fired"] == 1                               # screen did not block it
    assert row["s15_pass"] == 0                            # but it fails LUKEWARM
    assert "CHEAP" in row["s15_codes"] and "LUKEWARM" not in row["s15_codes"]
    assert row["s15_version"] == fifteen_min.S15_VERSION


def test_runner_screen_does_not_force_a_fire(tmp_path):
    """A LUKEWARM 15M NO PASSES the screen but still ABSTAINS (the existing gate needs
    confidence >= 0.55). s15_pass=1 must not turn an abstain into a fire."""
    r = _runner(tmp_path)
    cand = _cand(selected_probability=0.54, entry_ask_cents=55.0, total_cost_cents=0.0)
    verdict = gate.evaluate(cand, r.config)
    assert verdict["fired"] is False and "CONF_BELOW_MIN" in verdict["reason_codes"]
    row = r._build_row(cand, verdict, "15M", 900, 5, 1000.0,
                       record_kind="DELIVERED_CANDIDATE", delivery_status="PENDING")
    assert row["fired"] == 0                               # still an abstain
    assert row["s15_pass"] == 1                            # screen would have taken it
    assert set(row["s15_codes"].split(",")) == {"LUKEWARM", "CHEAP", "CAL_DRIFT", "FRESH"}


def test_runner_inert_at_other_marks(tmp_path):
    r = _runner(tmp_path)
    cand = _cand(selected_probability=0.54, entry_ask_cents=55.0)
    verdict = gate.evaluate(cand, r.config)
    row = r._build_row(cand, verdict, "10M", 600, 5, 1000.0,
                       record_kind="DELIVERED_CANDIDATE", delivery_status="PENDING")
    assert row["s15_pass"] is None and row["s15_version"] is None and row["s15_codes"] is None
