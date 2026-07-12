from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from q15_upgrade.drift_soak import (
    DEFAULT_ASSETS,
    MARK_OFFSET_SECONDS,
    WINDOW_SECONDS,
    audit_capture_coverage,
    build_soak_report,
    expected_window_keys,
    parse_epoch,
)
from tools.drift_soak_report import main


def _make_interval_db(path: Path, start: float, now: float, *, miss_last=False) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE interval_captures (model_version TEXT, interval TEXT, "
            "window_key INTEGER, captured_at REAL, asset TEXT, predicted_side TEXT, "
            "missing_reason TEXT)"
        )
        keys = expected_window_keys(start, now)
        for key in keys:
            for asset in DEFAULT_ASSETS:
                if miss_last and key == keys[-1] and asset == DEFAULT_ASSETS[-1]:
                    continue
                conn.execute(
                    "INSERT INTO interval_captures VALUES (?,?,?,?,?,?,?)",
                    ("interval-research-v1", "13M", key, start + 1, asset, "YES", None),
                )


def _make_strategy_db(
    path: Path, start: float, now: float, *, stale=False, overdue=False
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE strategy_bot_decisions (id INTEGER, created_at REAL, "
            "bot_name TEXT, decision_status TEXT, source_system TEXT, asset TEXT, "
            "ticker TEXT, close_time REAL, official_result TEXT, spot_depth_status TEXT, "
            "spot_depth_age_seconds REAL, spot_depth_trade_age_seconds REAL)"
        )
        conn.execute(
            "INSERT INTO strategy_bot_decisions VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
            (
                start + 10,
                "drift_flow_spread_13m",
                "ACCEPTED",
                "drift_shadow",
                "SOL",
                "SOL-1",
                start + 800,
                None if overdue else "YES",
                "stale" if stale else "ok",
                20.0 if stale else 2.0,
                21.0 if stale else 3.0,
            ),
        )


def _make_outbox_db(path: Path, start: float, *, failure=False, pending=False) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE q15_telegram_outbox (id INTEGER, status TEXT, created_at REAL, "
            "expires_at REAL, last_error TEXT)"
        )
        conn.execute(
            "INSERT INTO q15_telegram_outbox VALUES (1,?,?,?,?)",
            ("DEAD_LETTER" if failure else "SENT", start + 20, None, "boom" if failure else None),
        )
        if pending:
            conn.execute(
                "INSERT INTO q15_telegram_outbox VALUES (2,'PENDING',?,?,NULL)",
                (start + 30, start + 800),
            )


def _make_log(path: Path, start: float, cycle: float) -> None:
    local = datetime.fromtimestamp(start)
    path.write_text(
        f"{local:%Y-%m-%d %H:%M:%S},000 INFO started\n"
        f"{local:%Y-%m-%d %H:%M:%S},100 WARNING Slow cycle: {cycle:.2f}s "
        "(slowest stage run_cycle 1.00s, unaccounted 0.00s)\n",
        encoding="utf-8",
    )


def _sources(tmp_path: Path, start: float, now: float, **failures):
    interval = tmp_path / "interval.sqlite3"
    strategy = tmp_path / "strategy.sqlite3"
    outbox = tmp_path / "outbox.sqlite3"
    log = tmp_path / "app.err.log"
    _make_interval_db(interval, start, now, miss_last=failures.get("miss_last", False))
    _make_strategy_db(
        strategy,
        start,
        now,
        stale=failures.get("stale", False),
        overdue=failures.get("overdue", False),
    )
    _make_outbox_db(
        outbox,
        start,
        failure=failures.get("delivery", False),
        pending=failures.get("pending", False),
    )
    _make_log(log, start, failures.get("cycle", 42.0))
    return interval, strategy, outbox, log


def test_expected_marks_wait_for_bounded_finalization():
    start = 10 * WINDOW_SECONDS + MARK_OFFSET_SECONDS
    assert expected_window_keys(start, start + 89) == []
    assert expected_window_keys(start, start + 90) == [10]
    assert expected_window_keys(start + 1, start + WINDOW_SECONDS + 90) == [11]


def test_parse_epoch_requires_timezone_for_iso():
    assert parse_epoch("2026-07-11T12:00:00Z") == 1783771200.0
    try:
        parse_epoch("2026-07-11T12:00:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive ISO timestamp should be rejected")


def test_good_25_hour_soak_passes(tmp_path):
    start = 1_800_000_000.0
    now = start + 25 * 3600
    interval, strategy, outbox, log = _sources(tmp_path, start, now)
    report = build_soak_report(
        start_epoch=start,
        now_epoch=now,
        interval_db_path=interval,
        strategy_db_path=strategy,
        outbox_db_path=outbox,
        cycle_log_path=log,
    )
    assert report["status"] == "PASS"
    assert report["all_checks_passed_now"] is True
    assert report["capture"]["complete_window_coverage_percent"] == 100.0
    assert report["freshness"]["violations"] == 0
    assert report["delivery"]["terminal_failures"] == 0
    assert report["settlements"]["overdue_decisions"] == 0
    assert report["cycles"]["max_cycle_seconds"] == 42.0


def test_failures_are_actionable_after_24_hours(tmp_path):
    start = 1_800_000_000.0
    now = start + 25 * 3600
    interval, strategy, outbox, log = _sources(
        tmp_path,
        start,
        now,
        miss_last=True,
        stale=True,
        overdue=True,
        delivery=True,
        pending=True,
        cycle=95.0,
    )
    report = build_soak_report(
        start_epoch=start,
        now_epoch=now,
        interval_db_path=interval,
        strategy_db_path=strategy,
        outbox_db_path=outbox,
        cycle_log_path=log,
        coverage_threshold_percent=100.0,
    )
    assert report["status"] == "FAIL"
    assert set(name for name, check in report["checks"].items() if not check["passed"]) == {
        "capture_coverage",
        "effective_freshness",
        "delivery_failures",
        "overdue_settlements",
        "max_cycle",
    }
    assert report["capture"]["missing_rows"] == 1
    assert report["capture"]["absent_rows"] == 1
    assert report["delivery"]["pending"] == 1


def test_before_24_hours_status_is_collecting_even_if_check_fails(tmp_path):
    start = 1_800_000_000.0
    now = start + 3 * 3600
    interval, strategy, outbox, log = _sources(
        tmp_path, start, now, stale=True, cycle=95.0
    )
    report = build_soak_report(
        start_epoch=start,
        now_epoch=now,
        interval_db_path=interval,
        strategy_db_path=strategy,
        outbox_db_path=outbox,
        cycle_log_path=log,
    )
    assert report["status"] == "COLLECTING"
    assert report["all_checks_passed_now"] is False
    assert report["minimum_remaining_hours"] == 21.0


def test_missing_source_is_reported_and_never_created(tmp_path):
    missing = tmp_path / "does-not-exist.sqlite3"
    result = audit_capture_coverage(
        missing, start_epoch=1000, now_epoch=100_000
    )
    assert result["available"] is False
    assert not missing.exists()


def test_cli_json_and_fail_exit_code(tmp_path, capsys):
    start = 1_800_000_000.0
    now = start + 25 * 3600
    interval, strategy, outbox, log = _sources(
        tmp_path, start, now, cycle=95.0
    )
    code = main(
        [
            "--start",
            str(start),
            "--now",
            str(now),
            "--interval-db",
            str(interval),
            "--strategy-db",
            str(strategy),
            "--outbox-db",
            str(outbox),
            "--cycle-log",
            str(log),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "FAIL"
    assert payload["read_only"] is True
