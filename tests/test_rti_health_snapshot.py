from __future__ import annotations

import threading
import time

from q15_upgrade.strategy_bots import runtime


def test_rti_health_snapshot_never_waits_for_full_scoreboard(monkeypatch):
    runtime._reset_rti_health_snapshot_for_tests()
    started = threading.Event()
    release = threading.Event()

    def slow_health():
        started.set()
        release.wait(timeout=3.0)
        return {
            "available": True,
            "paper_only": True,
            "id": "impulse_strength_v1",
            "status": "ACTIVE",
        }

    monkeypatch.setattr(runtime, "rti_path_13m_challenger_health", slow_health)
    began = time.perf_counter()
    warming = runtime.rti_path_13m_challenger_health_cached()
    elapsed = time.perf_counter() - began
    assert started.wait(timeout=1.0)
    assert elapsed < 0.75
    assert warming["status"] == "SCOREBOARD_SNAPSHOT_WARMING"
    assert warming["health_snapshot"]["refreshing"] is True
    assert warming["health_snapshot"]["stale_while_revalidate"] is True

    release.set()
    assert runtime._rti_health_snapshot_event.wait(timeout=2.0)
    ready = runtime.rti_path_13m_challenger_health_cached()
    assert ready["available"] is True
    assert ready["status"] == "ACTIVE"
    assert ready["health_snapshot"]["stale"] is False
    assert ready["health_snapshot"]["refreshing"] is False
    runtime._reset_rti_health_snapshot_for_tests()


def test_promotion_fails_closed_when_any_resolved_cost_is_unscoreable(
    monkeypatch,
):
    metrics = {
        "rows": 30,
        "resolved": 30,
        "pnl_scoreable_resolved": 29,
        "unscoreable_resolved": 1,
        "cost_evidence_complete": False,
        "correct": 24,
        "accuracy": 0.8,
        "wilson_95_low": 0.65,
        "wilson_95_high": 0.90,
        "avg_fee_adjusted_breakeven_rate": 0.58,
        "avg_fee_slippage_adjusted_breakeven_rate": 0.60,
        "fee_adjusted_net_pnl_cents": 100.0,
        "max_cumulative_drawdown_cents": 20.0,
        "provisional": False,
    }
    book = {
        "policy_version": "test",
        "evaluated": 30,
        "qualified": 30,
        "qualification_rate": 1.0,
        "notification_eligible": True,
        "overall": metrics,
        "by_transfer_cohort": {
            "BTC": metrics,
            "NON_BTC_TRANSFER": {**metrics, "resolved": 0},
        },
        "rejected_counterfactual": {},
    }

    class FakeLedger:
        def rti_path_challenger_scoreboard(self, *_args, **_kwargs):
            return {
                "books": {"impulse_strength_v1": book},
                "probability_scorecards": {},
                "exact_feature_coverage": {},
            }

    monkeypatch.setattr(runtime, "get_ledger", lambda: FakeLedger())
    health = runtime.rti_path_13m_challenger_health()
    assert health["promotion_criteria_met"] is False
    assert health["promotion_criteria_by_cohort"]["BTC"] is False
    assert health["cost_evidence_complete"] is False
    assert health["unscoreable_resolved"] == 1
