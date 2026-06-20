"""Q15 V5 hardening regression tests.

Covers the fail-closed invariant: when the V5 freshness gate marks a snapshot's
market data invalid, the scalp engine must take NO action (no new scalp, no
resolution) and therefore emit no Telegram alert. Also pins the paper-only
nature of risk_preview and the suppression contract of enforce_fail_closed.
"""
from __future__ import annotations

import types

from scalp import ScalpEngine
from q15_upgrade.v5_hardening import (
    apply_snapshot_freshness,
    enforce_fail_closed,
    risk_preview,
)


class _Notifier:
    def __init__(self):
        self.enabled = True
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return True


class _Store:
    """Records any write so a leak past the gate is detectable."""

    def __init__(self):
        self.calls = []

    def claim_event(self, key):
        self.calls.append(("claim_event", key))
        return True

    def insert_signal(self, row):
        self.calls.append(("insert_signal", row))
        return 1

    def update_outcome(self, sid, outcome, realized):
        self.calls.append(("update_outcome", sid, outcome, realized))

    def execute(self, *a):
        self.calls.append(("execute", a))


def _cfg(**over):
    base = dict(scalp_enabled=True, alerts_enabled=True, scalp_cooldown_s=30.0,
               scalp_min_hold_secs=0.0)
    base.update(over)
    return types.SimpleNamespace(**base)


def _engine():
    return ScalpEngine(_Store(), _Notifier(), _cfg())


def test_invalid_snapshot_blocks_all_scalp_action():
    eng = _engine()
    calls = {"open": 0, "resolve": 0}
    eng._maybe_open = lambda *a, **k: calls.__setitem__("open", calls["open"] + 1)
    eng._resolve = lambda *a, **k: calls.__setitem__("resolve", calls["resolve"] + 1)

    # An open scalp exists for BTC (would normally be resolved every cycle).
    eng.open["BTC"] = {"ticker": "BTC-T", "side": "YES"}

    snaps = {
        "BTC": {"v5_data_valid": False, "v5_data_invalid_reasons": ["spot_stale_9.0s"]},
        "ETH": {"signal_suppressed": True, "suppression_reason": "v5_data_invalid"},
    }
    eng.evaluate(snaps, now=1000.0)

    assert calls == {"open": 0, "resolve": 0}
    assert eng.notifier.sent == []
    assert eng.store.calls == []
    assert "BTC" in eng.open  # position held, not resolved on bad data


def test_valid_snapshot_allows_scalp_action():
    eng = _engine()
    calls = {"open": 0, "resolve": 0}
    eng._maybe_open = lambda *a, **k: calls.__setitem__("open", calls["open"] + 1)
    eng._resolve = lambda *a, **k: calls.__setitem__("resolve", calls["resolve"] + 1)

    eng.open["ETH"] = {"ticker": "ETH-T", "side": "NO"}
    snaps = {
        "BTC": {"v5_data_valid": True},   # no open -> _maybe_open
        "ETH": {"v5_data_valid": True},   # open -> _resolve
    }
    eng.evaluate(snaps, now=1000.0)

    assert calls["open"] == 1   # BTC
    assert calls["resolve"] == 1  # ETH


def test_missing_flag_does_not_block_legacy_snapshots():
    # Backward-compat: a snapshot that never went through the V5 gate (no flag)
    # is not treated as invalid.
    eng = _engine()
    calls = {"open": 0}
    eng._maybe_open = lambda *a, **k: calls.__setitem__("open", calls["open"] + 1)
    eng.evaluate({"BTC": {}}, now=1000.0)
    assert calls["open"] == 1


def test_enforce_fail_closed_suppresses_entry():
    invalid = {"v5_data_valid": False, "entry_side": "YES",
               "entry_status": "ENTER", "decision_state": "ENTER"}
    out = enforce_fail_closed(invalid)
    assert out["entry_allowed"] is False
    assert out["signal_suppressed"] is True
    assert out["entry_side"] is None
    assert out["decision_state"] == "NO TRADE"


def test_enforce_fail_closed_passes_valid_through():
    valid = {"v5_data_valid": True, "entry_side": "YES", "decision_state": "ENTER"}
    out = enforce_fail_closed(valid)
    assert out.get("signal_suppressed") is not True
    assert out["entry_side"] == "YES"


def test_risk_preview_is_paper_only():
    snap = {"entry_side": "YES", "entry_ask": 40.0, "entry_fee": 1.0,
            "spread": 1.0, "yes_ask_qty": 1000.0}
    out = risk_preview(snap)
    assert out["paper_only"] is True
    assert out["paper_suggested_contracts"] >= 0
    assert "paper_worst_case_loss" in out
    # No execution/order field is ever introduced by the paper sizer.
    for key in out:
        assert "order" not in key.lower()


def test_apply_snapshot_freshness_flags_crossed_book():
    now = 1000.0
    raw = {"source": "rest", "spot": {"ok": True, "ts": now},
           "fetch_completed_at": now, "trades": []}
    snap = {"yes_bid": 60.0, "yes_ask": 55.0}  # crossed
    out = apply_snapshot_freshness(snap, raw, now)
    assert out["v5_data_valid"] is False
    assert "crossed_orderbook" in out["v5_data_invalid_reasons"]
