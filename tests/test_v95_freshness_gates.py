"""V9.5 freshness + checkpoint-labelling gates.

Three gates that were silently inert on the live path:

* ``quote_age`` was resolved from four key names nothing writes, so the
  ``stale_kalshi_quote`` entry blocker and the liquidity age decay never ran.
* ``apply_v95_policy`` unconditionally overwrote the v5 fail-closed verdict, so
  a snapshot v5 had judged stale could still come back ENTRY_RECOMMENDED.
* ``_resolve_checkpoint`` took ``max()`` over every snapshot, so one asset on an
  unopened market pinned the whole cycle's label to 15M.
"""
from __future__ import annotations

import time

from q15_upgrade.checkpoint_v95 import (
    _resolve_checkpoint,
    _source_timestamp,
    apply_v95_policy,
)


# ------------------------------------------------------- quote-age plumbing

def test_quote_age_resolves_from_the_key_v5_actually_writes():
    """v5_hardening writes ``orderbook_event_ts``; the legacy aliases are written
    by nothing, which is why quote_age was None on every live cycle."""
    now = time.time()
    snapshot = {"orderbook_event_ts": now - 42.0}

    ts = _source_timestamp(snapshot, ("orderbook_event_ts", "quote_timestamp",
                                      "market_quote_timestamp", "orderbook_timestamp",
                                      "kalshi_timestamp"))

    assert ts is not None
    assert 41.0 <= (now - ts) <= 43.0


def test_legacy_quote_alias_still_wins_when_present():
    """Back-compat: an explicit quote_timestamp is still honoured."""
    snapshot = {"quote_timestamp": 1000.0}
    assert _source_timestamp(snapshot, ("orderbook_event_ts", "quote_timestamp")) == 1000.0


# ------------------------------------------------- v5 fail-closed preservation

def _analysis(entry_allowed=True, decision="ENTRY_RECOMMENDED"):
    return {"entry_allowed": entry_allowed, "trade_decision": decision,
            "prediction_side": "NO", "prediction_available": True}


def test_v95_cannot_reopen_an_entry_v5_closed():
    """The core defect: v5 judged the data invalid, v9.5 re-blessed it anyway."""
    snapshot = {"v5_data_valid": False, "entry_allowed": False,
                "decision_state": "NO TRADE"}

    out = apply_v95_policy(snapshot, _analysis())

    assert out["entry_allowed"] is False
    assert out["new_entry_allowed"] is False
    # ...and the panel must not still claim an entry it is not allowing.
    assert out["decision_state"] == "AVOID_INVALID_DATA"


def test_v95_decision_is_unchanged_when_v5_says_the_data_is_valid():
    snapshot = {"v5_data_valid": True}

    out = apply_v95_policy(snapshot, _analysis())

    assert out["entry_allowed"] is True
    assert out["decision_state"] == "ENTRY_RECOMMENDED"


def test_v95_decision_is_unchanged_when_v5_never_ran():
    """Snapshots with no v5 verdict (older callers / tests) keep prior behaviour."""
    out = apply_v95_policy({}, _analysis())

    assert out["entry_allowed"] is True
    assert out["decision_state"] == "ENTRY_RECOMMENDED"


def test_v95_may_still_narrow_a_valid_snapshot():
    """v9.5 refusing an entry on fresh data is untouched — it may narrow, not widen."""
    out = apply_v95_policy({"v5_data_valid": True},
                           _analysis(entry_allowed=False, decision="WATCH_LIQUIDITY"))

    assert out["entry_allowed"] is False
    assert out["decision_state"] == "WATCH_LIQUIDITY"


# --------------------------------------------------- checkpoint label resolution

def _snap(seconds, state="live"):
    return {"seconds_remaining": seconds, "market_state": state}


def test_upcoming_market_cannot_pin_the_label_to_15m():
    """One asset with no current 15m market carries the SOONEST future close — tens
    of minutes out. It used to drag max() up and label every asset 15M."""
    now = time.time()
    snapshots = {
        "BTC": _snap(400.0),
        "ETH": _snap(402.0),
        "HYPE": _snap(2400.0, state="upcoming"),   # 40 minutes out, not open yet
    }

    assert _resolve_checkpoint(snapshots, [], now) == "7M"


def test_live_markets_still_drive_the_label():
    now = time.time()
    snapshots = {"BTC": _snap(700.0), "ETH": _snap(400.0)}

    # 700s is above the 660s 15M boundary, so the cycle is a 15M checkpoint.
    assert _resolve_checkpoint(snapshots, [], now) == "15M"


def test_over_long_time_is_ignored_even_when_marked_live():
    """A 15-minute window is 900s by construction; anything longer is not this window."""
    now = time.time()
    snapshots = {"BTC": _snap(500.0), "SOL": _snap(3000.0)}

    assert _resolve_checkpoint(snapshots, [], now) == "10M"


def test_falls_back_when_no_snapshot_declares_market_state():
    """Older callers and tests pass no market_state — behaviour must not regress."""
    now = time.time()
    snapshots = {"BTC": {"seconds_remaining": 400.0}, "ETH": {"seconds_remaining": 402.0}}

    assert _resolve_checkpoint(snapshots, [], now) == "7M"
