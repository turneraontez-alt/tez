"""Ultoim V2 — the research population must derive 'passed the gate' from
``gate_b_pass AND gate_c_pass`` (authoritative, present on every row), not from the
delivered ``fired`` flag.

Regression guard for the YES-side undercount: ``fired`` is structurally 0 for every
YES research row, and the ``research_fired`` column was backfilled to 0 (not NULL) by
the schema migration on rows written before it existed. A YES row whose
``research_fired`` was thus absent/backfilled must STILL count as research/gated,
because its original-schema ``gate_b_pass``/``gate_c_pass`` say it cleared the
side-agnostic gate. Otherwise the YES-side and regime N that qualify the promotion
verdict look smaller than they really are.

Deterministic: no network, no real clock (settlement timestamps passed explicitly),
tmp_path SQLite only.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import validate  # noqa: E402
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger


# --------------------------------------------------------------------------- #
# validate._is_gated / validate.gated_resolved (plain row dicts)
# --------------------------------------------------------------------------- #
def _gate_row(**over):
    """A settled YES research row dict, as ``resolved_rows`` would yield it. Defaults
    to the pre-migration/backfilled case: research_fired=0 but the gate passed."""
    row = {
        "predicted_side": "YES", "official_result": "NO", "correct": 0,
        "interval": "7M", "fired": 0, "research_fired": 0,
        "gate_b_pass": 1, "gate_c_pass": 1, "hypothetical_pnl_cents": -45.0,
    }
    row.update(over)
    return row


def test_is_gated_gate_cols_override_backfilled_research_fired():
    # 1. The backfilled/pre-migration YES case: gate_b/c=1, research_fired=0 -> COUNTS.
    row = _gate_row(gate_b_pass=1, gate_c_pass=1, research_fired=0)
    assert validate._is_gated(row) is True
    assert validate.gated_resolved([row]) == [row]


def test_is_gated_failed_gate_does_not_count():
    # 2. gate_b_pass=0 (gate genuinely failed), gate_c_pass=1 -> does NOT count, even
    #    though one leg passed. research_fired=0 must not rescue it.
    row = _gate_row(gate_b_pass=0, gate_c_pass=1, research_fired=0)
    assert validate._is_gated(row) is False
    assert validate.gated_resolved([row]) == []


def test_is_gated_falls_back_to_research_fired_when_gate_cols_absent():
    # 3. gate columns absent (None) but research_fired=1 -> COUNTS (back-compat with
    #    existing _vrow fixtures, which set research_fired but not gate_b/c).
    present = {"predicted_side": "YES", "official_result": "NO", "correct": 0,
               "research_fired": 1, "fired": 0}
    assert validate._is_gated(present) is True
    absent_none = dict(present, gate_b_pass=None, gate_c_pass=None)
    assert validate._is_gated(absent_none) is True
    # research_fired=0 with no gate columns -> does NOT count.
    assert validate._is_gated(dict(present, research_fired=0)) is False


def test_is_gated_oldest_fallback_to_fired():
    # 4. gate columns + research_fired all absent -> oldest fallback to ``fired``.
    assert validate._is_gated({"fired": 1, "official_result": "NO"}) is True
    assert validate._is_gated({"fired": 0, "official_result": "NO"}) is False
    assert validate._is_gated({"official_result": "NO"}) is False  # fired absent -> 0


# --------------------------------------------------------------------------- #
# ledger scoreboard research population (sqlite3.Row, end-to-end)
# --------------------------------------------------------------------------- #
def _led_row(**over):
    """A minimal, fully-specified ledger row for record_decision. Defaults model the
    backfilled YES research case (fired=0, gate_b/c=1, research_fired=0)."""
    row = {
        "created_at": 1000.0, "model_version": "ultoim-v2", "asset": "BTC",
        "ticker": "T-BTC", "interval": "10M", "window_key": 5, "fired": 0,
        "predicted_side": "YES", "entry_ask_cents": 60.0, "regime_directional": "YES_PRONE",
        "gate_a_pass": 1, "gate_b_pass": 1, "gate_c_pass": 1, "research_fired": 0,
        "close_time": 9000.0, "delivery_status": "SUPPRESSED",
        "record_kind": "RESEARCH_YES",
    }
    row.update(over)
    return row


def test_scoreboard_counts_backfilled_yes_research_row(tmp_path):
    # 5. End-to-end: a YES research row whose research_fired was backfilled to 0 (the
    #    pre-migration shape) must still land in the research population — both in
    #    by_side["YES"] and in research_resolved — because gate_b/c=1.
    led = UltoimV2Ledger(str(tmp_path / "t.sqlite3"))
    # YES research row: never delivered (fired=0), gate passed, research_fired backfilled to 0.
    led.record_decision(_led_row(
        ticker="Y1", interval="7M", predicted_side="YES", fired=0,
        gate_b_pass=1, gate_c_pass=1, research_fired=0, record_kind="RESEARCH_YES"))
    # Normal delivered NO entry: fired=1, gate passed.
    led.record_decision(_led_row(
        ticker="N1", interval="10M", predicted_side="NO", fired=1,
        gate_b_pass=1, gate_c_pass=1, research_fired=1, entry_ask_cents=55.0,
        regime_directional="NO_PRONE", delivery_status="SENT",
        record_kind="DELIVERED_CANDIDATE"))
    led.resolve("ultoim-v2", "Y1", "NO", 9500.0)   # YES predicted, settles NO -> loss
    led.resolve("ultoim-v2", "N1", "NO", 9500.0)   # NO predicted, settles NO -> win

    sb = led.scoreboard("ultoim-v2", min_n=1)
    # The YES research row is recovered into the research population (was undercounted).
    assert sb["by_side"]["YES"]["n"] == 1 and sb["by_side"]["YES"]["right"] == 0
    assert sb["by_side"]["NO"]["n"] == 1 and sb["by_side"]["NO"]["right"] == 1
    assert sb["research_resolved"] == 2
    # The YES research row never inflates the delivered headline (fired==1 only).
    assert sb["overall"]["n"] == 1 and sb["resolved"] == 1
    led.close()


def test_scoreboard_excludes_failed_gate_row(tmp_path):
    # A row that genuinely failed the gate (gate_b_pass=0) is NOT research, regardless
    # of research_fired, so it stays out of the research population.
    led = UltoimV2Ledger(str(tmp_path / "t.sqlite3"))
    led.record_decision(_led_row(
        ticker="F1", interval="7M", predicted_side="YES", fired=0,
        gate_b_pass=0, gate_c_pass=1, research_fired=0))
    led.resolve("ultoim-v2", "F1", "NO", 9500.0)
    sb = led.scoreboard("ultoim-v2", min_n=1)
    assert sb["research_resolved"] == 0
    assert sb["by_side"]["YES"]["n"] == 0
    led.close()
