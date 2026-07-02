from __future__ import annotations

import time

from q15_upgrade.ledger_v95 import V95Ledger
from tools.backfill_resolutions import backfill_resolutions


def _record_closed(ledger: V95Ledger, ticker: str, *, close_time: float, checkpoint: str = "10M") -> None:
    ledger.record_prediction(
        ticker=ticker,
        asset="ETH",
        checkpoint=checkpoint,
        created_at=close_time - 900,
        close_time=close_time,
        predicted_side="YES",
        raw_yes_probability=0.7,
        calibrated_yes_probability=0.7,
        challenger_yes_probability=0.7,
        baseline_yes_probability=0.6,
        selected_probability=0.7,
        conservative_probability=0.65,
        data_quality=0.8,
        evidence_quality=0.7,
        trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED",
        regime="NORMAL",
        features={"momentum": 0.3},
        contributions={"momentum": 0.2},
        quote={"ask_cents": 50},
        rank=1,
    )


def test_backfill_dry_run_does_not_grade_rows(tmp_path):
    now = time.time()
    ledger = V95Ledger(tmp_path / "ledger.sqlite3")
    _record_closed(ledger, "YES-TICKER", close_time=now - 100)
    _record_closed(ledger, "UNKNOWN-TICKER", close_time=now - 90)

    summary = backfill_resolutions(
        ledger=ledger,
        get_market=lambda ticker: {"result": "YES"} if ticker == "YES-TICKER" else {"result": "", "status": "closed"},
        now=now,
        dry_run=True,
        emit=lambda _msg: None,
    )

    assert summary["dry_run"] is True
    assert summary["tickers_examined"] == 2
    assert summary["new_predictions_resolved"] == 1
    assert summary["remaining_undetermined"][0]["ticker"] == "UNKNOWN-TICKER"
    assert ledger.scoreboard()["overall"]["n"] == 0


def test_backfill_real_run_resolves_idempotently_and_reports_shadow_updates(tmp_path):
    now = time.time()
    ledger = V95Ledger(tmp_path / "ledger.sqlite3")
    _record_closed(ledger, "YES-TICKER", close_time=now - 100)
    _record_closed(ledger, "NO-TICKER", close_time=now - 90)

    results = {"YES-TICKER": {"result": "YES"}, "NO-TICKER": {"result": "NO"}}
    summary = backfill_resolutions(
        ledger=ledger,
        get_market=lambda ticker: results[ticker],
        now=now,
        emit=lambda _msg: None,
    )
    repeat = backfill_resolutions(
        ledger=ledger,
        get_market=lambda ticker: results[ticker],
        now=now,
        emit=lambda _msg: None,
    )

    assert summary["new_predictions_resolved"] == 2
    assert "shadow_predictions_resolved" in summary
    assert summary["remaining_undetermined"] == []
    assert ledger.scoreboard()["overall"]["n"] == 2
    assert repeat["tickers_examined"] == 0
    assert repeat["new_predictions_resolved"] == 0


def test_backfill_retry_parked_requeues_before_run(tmp_path):
    now = time.time()
    ledger = V95Ledger(tmp_path / "ledger.sqlite3")
    _record_closed(ledger, "PARKED", close_time=now - 100)
    ledger.reconcile_pending_from_market(lambda _ticker: {"result": "", "status": "closed"}, now=now, max_calls=1)
    ledger.requeue_parked_reconcile_tickers(["PARKED"])
    ledger.reconcile_pending_from_market(lambda _ticker: {"result": "", "status": "closed"}, now=now, max_calls=1)

    # Force a parked row without relying on the default 10-pass threshold.
    with ledger._lock, ledger._connect() as connection:
        connection.execute(
            "UPDATE reconcile_skip SET consecutive_undetermined=10, parked_at=? WHERE ticker='PARKED'",
            (now,),
        )
        connection.commit()

    summary = backfill_resolutions(
        ledger=ledger,
        get_market=lambda _ticker: {"result": "YES", "close_time": now - 100},
        now=now,
        retry_parked=True,
        emit=lambda _msg: None,
    )

    assert summary["requeued_parked"] == 1
    assert summary["new_predictions_resolved"] == 1
