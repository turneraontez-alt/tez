"""Entry-policy regression tests for the adaptive confirmation tier.

Primary path: >= min_confirmation_groups (3) at score >= min_confirmation_score
(48). Adaptive path: a setup with only adaptive_min_groups (2) may still qualify
if it clears a stricter bar -- score >= 62, edge >= required + 2c, and model
quality >= 0.65. Tests drive ``_entry_blockers`` directly with stand-in objects.
"""
from __future__ import annotations

from types import SimpleNamespace

from q15_upgrade.config import Q15Settings
from q15_upgrade.runtime import Q15Runtime


class _Learner:
    def adjustment_for(self, *args):
        return {"edge_adjustment": 0.0, "suppressed": False, "reasons": []}


def _runtime(**over):
    return Q15Runtime(learner=_Learner(), settings=Q15Settings(**over))


def _blockers(rt, *, groups, score, model=0.70, prob_yes=0.60, win=0.70,
              edge=10.0, required_edge=5.0, spread=1.0, depth=100.0):
    return rt._entry_blockers(
        snap={"market_state": "live"},
        follow=SimpleNamespace(follow_through=True, invalidated=False, reason=None),
        side="YES",
        seconds=600,
        confirmations=SimpleNamespace(active_group_count=groups, total_score=score),
        estimate=SimpleNamespace(probability_yes=prob_yes, confidence=model),
        win_prob=win,
        edge=edge,
        required_edge=required_edge,
        spread=spread,
        depth=depth,
        full_size_available=True,
        data_warning=False,
        learned={"suppressed": False},
    )


def test_policy_defaults():
    s = Q15Settings()
    assert s.candidate_persistence_seconds == 8.0   # ENTRY_STABILITY_SECONDS
    assert s.entry_stability_samples == 4            # ENTRY_STABILITY_SAMPLES
    assert s.min_new_entry_seconds == 60
    assert s.min_win_probability == 0.55
    assert s.min_model_confidence == 0.55
    assert s.max_spread_cents == 3.0
    assert s.min_depth_contracts == 20.0
    assert s.wide_spread_min_depth == 35.0
    assert s.min_confirmation_groups == 3
    assert s.min_confirmation_score == 48.0
    assert s.adaptive_confirmation_enabled is True
    assert s.adaptive_min_groups == 2
    assert s.adaptive_min_score == 62.0
    assert s.adaptive_extra_edge_cents == 2.0
    assert s.adaptive_min_model_quality == 0.65
    # Edge tiers preserved.
    assert (s.edge_12_15m, s.edge_6_12m, s.edge_2_6m, s.edge_60_120s) == (8.0, 6.0, 5.0, 8.0)


def test_primary_path_passes_at_score_48():
    rt = _runtime()
    # 3 groups, score exactly 48, base edge, model 0.55 -> clean.
    assert _blockers(rt, groups=3, score=48.0, model=0.55,
                     edge=5.0, required_edge=5.0) == []


def test_primary_path_blocks_below_score_48():
    rt = _runtime()
    out = _blockers(rt, groups=3, score=47.0)
    assert "confirmation_score_below_48" in out


def test_adaptive_path_passes_with_two_groups():
    rt = _runtime()
    # 2 groups but stricter bar met: score>=62, model>=0.65, edge>=required+2.
    out = _blockers(rt, groups=2, score=62.0, model=0.65,
                    edge=7.0, required_edge=5.0)
    assert out == []


def test_adaptive_path_requires_higher_score():
    rt = _runtime()
    out = _blockers(rt, groups=2, score=61.0, model=0.70,
                    edge=7.0, required_edge=5.0)
    assert "confirmation_score_below_62" in out


def test_adaptive_path_requires_extra_edge():
    rt = _runtime()
    # edge only meets base requirement, not the +2c adaptive margin.
    out = _blockers(rt, groups=2, score=65.0, model=0.70,
                    edge=6.0, required_edge=5.0)
    assert "edge_below_required_7.0c" in out


def test_adaptive_path_requires_higher_model_quality():
    rt = _runtime()
    out = _blockers(rt, groups=2, score=65.0, model=0.60,
                    edge=7.0, required_edge=5.0)
    assert "model_quality_below_0.65" in out


def test_below_adaptive_groups_blocks():
    rt = _runtime()
    out = _blockers(rt, groups=1, score=90.0, model=0.90,
                    edge=20.0, required_edge=5.0)
    assert "need_2_independent_groups" in out


def test_adaptive_disabled_falls_back_to_three_groups():
    rt = _runtime(adaptive_confirmation_enabled=False)
    out = _blockers(rt, groups=2, score=90.0, model=0.90,
                    edge=20.0, required_edge=5.0)
    assert "need_3_independent_groups" in out
