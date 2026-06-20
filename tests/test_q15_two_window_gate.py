"""Safety-contract tests for the Q15 two-window entry gate wrapper.

The wrapper (1) hard-blocks entry when the focus manager has not promoted the
market (`q15_new_entry_allowed is False`), and (2) when an adaptive confirmation
is accepted, may relax ONLY the confirmation group/score blockers -- it must
never strip data-freshness, spread, depth, win-probability, edge, or learned
suppression blockers, so an entry can never slip through on invalid/stale data.
"""
from __future__ import annotations

from q15_upgrade.config import Q15Settings
from q15_upgrade.runtime import Q15Runtime
import q15_upgrade.runtime as runtime_mod


class _Learner:
    def adjustment_for(self, *args):
        return {"edge_adjustment": 0.0, "suppressed": False, "reasons": []}


def _runtime(**over):
    return Q15Runtime(learner=_Learner(), settings=Q15Settings(**over))


SAFETY_BLOCKERS = [
    "stale_snapshot",
    "spread_too_wide",
    "thin_best_ask_depth",
    "win_probability_below_0.55",
    "edge_below_required_5.0c",
    "learned_segment_suppression",
    "market_not_live",
]
# Only blockers whose text contains "confirmation" AND ("group"|"score") are
# relaxed. The inner fn emits the score blocker as "confirmation_score_below_N"
# (strippable) but the group blocker as "need_N_independent_groups" -- which
# lacks the word "confirmation" and is therefore NEVER relaxed (conservative).
CONFIRMATION_STRIPPABLE = [
    "confirmation_score_below_48",
]
CONFIRMATION_PRESERVED = [
    "need_3_independent_groups",
]


def test_focus_gate_blocks_when_not_promoted():
    rt = _runtime()
    out = rt._entry_blockers(snap={"q15_new_entry_allowed": False})
    assert out == ["two_prediction_focus_gate"]


def test_focus_gate_passes_through_when_allowed(monkeypatch):
    rt = _runtime()
    monkeypatch.setattr(rt, "_entry_blockers_without_two_window",
                        lambda *a, **k: ["stale_snapshot"])
    monkeypatch.setattr(runtime_mod, "adaptive_confirmation_ok", lambda **k: False)
    out = rt._entry_blockers(snap={"q15_new_entry_allowed": True})
    assert out == ["stale_snapshot"]


def test_adaptive_accept_strips_only_confirmation_score_blocker(monkeypatch):
    rt = _runtime()
    fixed = list(CONFIRMATION_STRIPPABLE) + list(CONFIRMATION_PRESERVED) + list(SAFETY_BLOCKERS)
    monkeypatch.setattr(rt, "_entry_blockers_without_two_window",
                        lambda *a, **k: list(fixed))
    monkeypatch.setattr(runtime_mod, "adaptive_confirmation_ok", lambda **k: True)
    out = rt._entry_blockers(snap={})
    # Only the confirmation score blocker is relaxed.
    for c in CONFIRMATION_STRIPPABLE:
        assert c not in out
    # The group-count blocker lacks "confirmation" and is preserved.
    for keep in CONFIRMATION_PRESERVED:
        assert keep in out
    # Every data-safety / risk blocker survives.
    for keep in SAFETY_BLOCKERS:
        assert keep in out


def test_no_stripping_when_adaptive_rejects(monkeypatch):
    rt = _runtime()
    fixed = list(CONFIRMATION_STRIPPABLE) + list(CONFIRMATION_PRESERVED) + list(SAFETY_BLOCKERS)
    monkeypatch.setattr(rt, "_entry_blockers_without_two_window",
                        lambda *a, **k: list(fixed))
    monkeypatch.setattr(runtime_mod, "adaptive_confirmation_ok", lambda **k: False)
    out = rt._entry_blockers(snap={})
    assert out == fixed


def test_stale_data_still_blocks_even_with_adaptive_accept(monkeypatch):
    # Architect contract: an adaptive-eligible setup on invalid/stale data must
    # still be blocked -- the stale_snapshot blocker is never relaxed.
    rt = _runtime()
    monkeypatch.setattr(rt, "_entry_blockers_without_two_window",
                        lambda *a, **k: ["confirmation_score_below_48", "stale_snapshot"])
    monkeypatch.setattr(runtime_mod, "adaptive_confirmation_ok", lambda **k: True)
    out = rt._entry_blockers(snap={"q15_new_entry_allowed": True})
    assert out == ["stale_snapshot"]
