from __future__ import annotations

import json

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_NO_EXPANSION,
    BotDecision,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    drift_no_expansion_decision,
)


def _row(**over):
    base = {
        "created_at": 2000.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_NO_EXPANSION",
        "rule_code": "DRIFT_NO_EXPANSION_FLOW_SPREAD_V1",
        "reason_codes": "DRIFT_NO_EXPANSION_CANDIDATE,XRP_NO_60_69",
        "delivery_status": "PAPER_DRIFT_NO_EXPANSION",
        "asset": "XRP",
        "ticker": "KXXRP15M-NO-EXP",
        "interval": "13M",
        "window_key": 1200,
        "close_time": 4_102_444_800.0,
        "predicted_side": "NO",
        "entry_ask_cents": 67.0,
        "spread_cents": 3.0,
        "depth_contracts": 100.0,
        "distance_sigma": 1e-5,
        "flip_probability": 20.0,
        "spot_depth_status": "ok",
        "spot_depth_trade_net_notional_60s": -500.0,
    }
    base.update(over)
    return base


class _Telegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message: str):
        self.sent.append(message)
        return {
            "delivered": True,
            "muted": False,
            "message_id": len(self.sent),
            "error": None,
        }


def test_no_expansion_decision_paths_and_asset_bands(monkeypatch):
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION", "true")

    flow = drift_no_expansion_decision(_row())
    assert flow is not None and flow.decision_status == RESEARCH_ONLY
    assert flow.bot_name == BOT_DRIFT_NO_EXPANSION
    assert flow.threshold_profile["gate_path"] == "FLOW_60S_NEGATIVE"
    assert flow.threshold_profile["counterfactual_status"] == ACCEPTED

    spread = drift_no_expansion_decision(_row(
        asset="HYPE",
        entry_ask_cents=63.0,
        spread_cents=2.0,
        spot_depth_trade_net_notional_60s=100.0,
    ))
    assert spread is not None and spread.decision_status == RESEARCH_ONLY
    assert spread.threshold_profile["gate_path"] == "SPREAD_LTE_2"
    assert spread.threshold_profile["counterfactual_status"] == ACCEPTED

    rejected = drift_no_expansion_decision(_row(
        asset="DOGE",
        entry_ask_cents=67.0,
        spread_cents=3.0,
        spot_depth_trade_net_notional_60s=100.0,
    ))
    assert rejected is not None and rejected.decision_status == RESEARCH_ONLY
    assert rejected.threshold_profile["counterfactual_status"] == REJECTED

    stale = drift_no_expansion_decision(_row(
        spread_cents=3.0,
        spot_depth_status="stale",
    ))
    assert stale is not None and stale.decision_status == RESEARCH_ONLY
    assert stale.threshold_profile["counterfactual_status"] == RESEARCH_ONLY
    for decision in (flow, spread, rejected, stale):
        assert decision.threshold_profile["never_notify"] is True
        assert decision.threshold_profile["notification_eligible"] is False
        assert decision.threshold_profile["passed"] is False

    assert drift_no_expansion_decision(_row(asset="SOL")) is None
    assert drift_no_expansion_decision(_row(asset="HYPE", entry_ask_cents=65.0)) is None
    assert drift_no_expansion_decision(_row(asset="DOGE", entry_ask_cents=64.0)) is None
    assert drift_no_expansion_decision(_row(distance_sigma=4e-5)) is None
    assert drift_no_expansion_decision(_row(flip_probability=31.0)) is None


def test_no_expansion_runtime_is_silent_even_when_legacy_notify_is_true(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", "true")
    runtime._ledger = None
    runtime._telegram = telegram
    monkeypatch.setattr(runtime, "enrich_spot_depth", lambda row: dict(row))
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))
    monkeypatch.setattr(runtime, "enrich_drift_evidence", lambda row: dict(row))

    rows = [
        _row(ticker="XRP-FLOW"),
        _row(
            asset="HYPE", ticker="HYPE-SPREAD", entry_ask_cents=63.0,
            spread_cents=2.0, spot_depth_trade_net_notional_60s=100.0,
        ),
        _row(
            asset="DOGE", ticker="DOGE-REJECT", entry_ask_cents=67.0,
            spread_cents=3.0, spot_depth_trade_net_notional_60s=100.0,
        ),
        _row(
            ticker="XRP-STALE", spread_cents=3.0, spot_depth_status="stale",
        ),
    ]
    row_ids = runtime.record_drift_no_expansion_window(rows)
    assert len(row_ids) == 4
    assert runtime.record_drift_no_expansion_window(rows) == []
    assert telegram.sent == []

    ledger = runtime.get_ledger()
    recorded = [
        row for row in ledger.rows(STRATEGY_VERSION)
        if row["bot_name"] == BOT_DRIFT_NO_EXPANSION
    ]
    assert {row["decision_status"] for row in recorded} == {RESEARCH_ONLY}
    assert {row["notification_status"] for row in recorded} == {None}
    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)["drift_system"]
    assert scoreboard["no_expansion"]["rows"] == 4
    assert scoreboard["no_expansion_accepted"]["rows"] == 0
    assert scoreboard["no_expansion_by_status"][RESEARCH_ONLY]["rows"] == 4
    shadow = scoreboard["counterfactual_research"]["no_expansion"]
    assert shadow["funnel"]["overall"]["rows"] == 4
    assert shadow["qualifiers"]["overall"]["rows"] == 2


def test_no_expansion_evidence_grade_is_relative_to_no_side(monkeypatch):
    monkeypatch.setenv("Q15_BUILD_SHA", "no-expansion-test-build")
    row = _row(
        drift_evidence_complete=True,
        drift_v91_yes_fraction_all=0.20,
        drift_v95_15m_side="NO",
        drift_v95_15m_flow_score=-1.0,
        drift_btc_15m_side="NO",
        drift_asymmetric_breadth=0,
        drift_flow_coverage=1.0,
        index_status="ok",
        kalshi_depth_status="ok",
    )
    enriched = runtime._with_drift_lineage_and_grade(
        row,
        expected_side="NO",
        lineage_config=runtime._drift_no_expansion_lineage_config(),
    )
    assert enriched["evidence_grade"] == "A"
    assert enriched["full_feature_complete"] is True
    assert json.loads(enriched["drift_evidence_json"])["expected_side"] == "NO"


def test_no_expansion_quarantine_preserves_legacy_accepted_exposure(tmp_path):
    ledger = StrategyBotLedger(tmp_path / "v3.sqlite3")
    try:
        legacy_row = _row(ticker="LEGACY-NO", window_key=1201)
        legacy = BotDecision(
            bot_name=BOT_DRIFT_NO_EXPANSION,
            decision_status=ACCEPTED,
            reason_codes=("LEGACY_NO_EXPANSION_ACCEPTED",),
            threshold_profile={
                "rule_version": "drift-no-expansion-13m-v1",
                "counts_as_independent_pick": True,
                "exposure_class": "INDEPENDENT_NO_EXPANSION",
            },
            side_override="NO",
            entry_ask_cents=67.0,
            use_entry_ask_override=True,
        )
        assert ledger.record_decision(
            legacy, legacy_row, source_system="drift_shadow"
        ) is not None

        shadow_row = _row(ticker="SHADOW-NO", window_key=1202)
        shadow = drift_no_expansion_decision(shadow_row)
        assert shadow is not None
        assert ledger.record_decision(
            shadow, shadow_row, source_system="drift_shadow"
        ) is not None

        scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)
        assert scoreboard["accepted"]["rows"] == 1
        assert scoreboard["all_exposure"]["rows"] == 1
        assert scoreboard["counterfactual_research_rows"] == 1
        no_expansion = scoreboard["drift_system"]["counterfactual_research"][
            "no_expansion"
        ]
        assert no_expansion["funnel"]["overall"]["rows"] == 2
        assert no_expansion["qualifiers"]["overall"]["rows"] == 1
    finally:
        ledger.close()
