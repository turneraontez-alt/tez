#!/usr/bin/env python3
"""Targeted Predexon depth backfill for PRE-80 decision checkpoints.

Unlike the full ticker backfill, this fetches narrow windows around specific
chart checkpoints. The goal is to collect depth that can be used without
future leakage at the exact decision times we care about.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PREDEXON_ORDERBOOKS_URL = "https://api.predexon.com/v2/kalshi/orderbooks"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_api_key(key_file: str | None) -> str:
    env_key = os.environ.get("PREDEXON_API_KEY", "").strip()
    if env_key:
        return env_key
    if not key_file:
        raise SystemExit("Missing PREDEXON_API_KEY or --key-file")
    lines = [
        line.strip()
        for line in Path(key_file).read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    ]
    if not lines:
        raise SystemExit(f"No usable key lines in {key_file}")
    return lines[1] if len(lines) >= 2 else lines[0]


def connect_out(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    con.execute(
        """
        create table if not exists orderbook_snapshots (
            ticker text not null,
            ts_ms integer not null,
            sequence integer,
            best_bid integer,
            best_ask integer,
            bid_depth integer,
            ask_depth integer,
            yes_bids_json text,
            yes_asks_json text,
            raw_json text not null,
            inserted_at text not null,
            primary key (ticker, ts_ms, sequence)
        )
        """
    )
    con.execute(
        """
        create table if not exists targeted_depth_targets (
            target_id text primary key,
            ticker text not null,
            asset text,
            interval text,
            window_key integer,
            captured_at real,
            close_time real,
            yes_bid_cents real,
            yes_ask_cents real,
            calibrated_yes_probability real,
            official_result text,
            start_time_ms integer not null,
            end_time_ms integer not null,
            status text not null default 'pending',
            attempts integer not null default 0,
            snapshot_count integer not null default 0,
            first_snapshot_ms integer,
            last_snapshot_ms integer,
            pagination_key text,
            last_error text,
            updated_at text
        )
        """
    )
    con.execute(
        """
        create table if not exists targeted_depth_events (
            id integer primary key autoincrement,
            created_at text not null,
            target_id text,
            ticker text,
            event text not null,
            detail text
        )
        """
    )
    con.commit()
    return con


def load_targets(
    source_db: Path,
    intervals: set[str],
    assets: set[str] | None,
    pre_seconds: int,
    post_seconds: int,
    ask_max: float,
    include_unresolved: bool,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(source_db)
    con.row_factory = sqlite3.Row
    where = [
        "prediction_available = 1",
        "predicted_side = 'YES'",
        "yes_ask_cents is not null",
        "yes_ask_cents < ?",
        "captured_at is not null",
        "close_time is not null",
    ]
    args: list[Any] = [ask_max]
    if intervals:
        placeholders = ",".join("?" for _ in intervals)
        where.append(f"interval in ({placeholders})")
        args.extend(sorted(intervals))
    if assets:
        placeholders = ",".join("?" for _ in assets)
        where.append(f"asset in ({placeholders})")
        args.extend(sorted(assets))
    if not include_unresolved:
        where.extend(["official_result is not null", "official_result != ''"])

    rows = con.execute(
        f"""
        select
            ticker, asset, interval, window_key, captured_at, close_time,
            yes_bid_cents, yes_ask_cents, calibrated_yes_probability, official_result
        from interval_captures
        where {" and ".join(where)}
        order by close_time desc, interval desc, asset asc
        """,
        args,
    ).fetchall()
    con.close()

    targets: list[dict[str, Any]] = []
    for row in rows:
        captured_at = float(row["captured_at"])
        close_time = float(row["close_time"])
        start = max(0.0, captured_at - pre_seconds)
        end = min(close_time, captured_at + post_seconds)
        if end <= start:
            continue
        target_id = f"{row['ticker']}|{row['interval']}|{int(captured_at)}|pre{pre_seconds}|post{post_seconds}"
        targets.append(
            {
                "target_id": target_id,
                "ticker": row["ticker"],
                "asset": row["asset"],
                "interval": row["interval"],
                "window_key": row["window_key"],
                "captured_at": captured_at,
                "close_time": close_time,
                "yes_bid_cents": row["yes_bid_cents"],
                "yes_ask_cents": row["yes_ask_cents"],
                "calibrated_yes_probability": row["calibrated_yes_probability"],
                "official_result": row["official_result"],
                "start_time_ms": int(start * 1000),
                "end_time_ms": int(end * 1000),
                "updated_at": utc_now_iso(),
            }
        )
    return targets


def seed_targets(con: sqlite3.Connection, targets: list[dict[str, Any]]) -> None:
    con.executemany(
        """
        insert or ignore into targeted_depth_targets (
            target_id, ticker, asset, interval, window_key, captured_at, close_time,
            yes_bid_cents, yes_ask_cents, calibrated_yes_probability, official_result,
            start_time_ms, end_time_ms, updated_at
        ) values (
            :target_id, :ticker, :asset, :interval, :window_key, :captured_at, :close_time,
            :yes_bid_cents, :yes_ask_cents, :calibrated_yes_probability, :official_result,
            :start_time_ms, :end_time_ms, :updated_at
        )
        """,
        targets,
    )
    con.commit()


def pending_targets(
    con: sqlite3.Connection,
    limit: int | None,
    retry_empty: bool,
    retry_partial: bool,
    newest_first: bool,
) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    statuses = ["pending", "rate_limited", "error"]
    if retry_empty:
        statuses.append("empty")
    if retry_partial:
        statuses.append("partial")
    placeholders = ",".join("?" for _ in statuses)
    close_sort = "close_time desc" if newest_first else "close_time asc"
    sql = f"""
        select * from targeted_depth_targets
        where status in ({placeholders})
        order by
            case status
                when 'pending' then 0
                when 'rate_limited' then 1
                when 'error' then 2
                when 'partial' then 3
                else 4
            end,
            {close_sort},
            interval desc,
            asset asc
    """
    args: list[Any] = list(statuses)
    if limit:
        sql += " limit ?"
        args.append(limit)
    return con.execute(sql, args).fetchall()


def build_url(target: sqlite3.Row, page_limit: int, pagination_key: str | None) -> str:
    params: dict[str, Any] = {
        "ticker": target["ticker"],
        "start_time": int(target["start_time_ms"]),
        "end_time": int(target["end_time_ms"]),
        "limit": page_limit,
    }
    if pagination_key:
        params["pagination_key"] = pagination_key
    return PREDEXON_ORDERBOOKS_URL + "?" + urllib.parse.urlencode(params)


def request_json(url: str, api_key: str, timeout: int) -> tuple[int, dict[str, Any] | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tez-predexon-targeted-depth/1.0",
            "x-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        return exc.code, None, body
    except Exception as exc:  # noqa: BLE001
        return 0, None, f"{type(exc).__name__}: {exc}"


def insert_snapshots(con: sqlite3.Connection, ticker: str, snapshots: list[dict[str, Any]]) -> int:
    inserted = 0
    now = utc_now_iso()
    for snap in snapshots:
        ts_ms = snap.get("timestamp")
        if ts_ms is None:
            continue
        cur = con.execute(
            """
            insert or ignore into orderbook_snapshots (
                ticker, ts_ms, sequence, best_bid, best_ask, bid_depth, ask_depth,
                yes_bids_json, yes_asks_json, raw_json, inserted_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                int(ts_ms),
                snap.get("sequence"),
                snap.get("best_bid"),
                snap.get("best_ask"),
                snap.get("bid_depth"),
                snap.get("ask_depth"),
                json.dumps(snap.get("yes_bids") or [], separators=(",", ":")),
                json.dumps(snap.get("yes_asks") or [], separators=(",", ":")),
                json.dumps(snap, separators=(",", ":")),
                now,
            ),
        )
        inserted += cur.rowcount
    return inserted


def update_target(
    con: sqlite3.Connection,
    target_id: str,
    ticker: str,
    start_ms: int,
    end_ms: int,
    status: str,
    seen_count: int,
    error: str | None,
    pagination_key: str | None,
) -> None:
    row = con.execute(
        """
        select min(ts_ms), max(ts_ms), count(*)
        from orderbook_snapshots
        where ticker = ? and ts_ms between ? and ?
        """,
        (ticker, start_ms, end_ms),
    ).fetchone()
    first_ms, last_ms, stored_count = row
    con.execute(
        """
        update targeted_depth_targets
        set status = ?,
            attempts = attempts + 1,
            snapshot_count = ?,
            first_snapshot_ms = ?,
            last_snapshot_ms = ?,
            pagination_key = ?,
            last_error = ?,
            updated_at = ?
        where target_id = ?
        """,
        (
            status,
            max(int(stored_count or 0), seen_count),
            first_ms,
            last_ms,
            pagination_key if status in {"partial", "rate_limited"} else None,
            error,
            utc_now_iso(),
            target_id,
        ),
    )


def fetch_target(
    con: sqlite3.Connection,
    target: sqlite3.Row,
    api_key: str,
    page_limit: int,
    max_pages: int,
    timeout: int,
    sleep_seconds: float,
) -> tuple[str, int, str | None]:
    pagination_key = target["pagination_key"]
    total_seen = 0
    total_inserted = 0
    final_status = "empty"
    final_error = None

    for _page in range(max_pages):
        url = build_url(target, page_limit, pagination_key)
        code, data, body = request_json(url, api_key, timeout)
        if code == 429:
            final_status = "rate_limited"
            final_error = "HTTP 429 Too Many Requests"
            break
        if code < 200 or code >= 300 or data is None:
            final_status = "error"
            final_error = f"HTTP {code}: {body or 'no body'}"
            break
        snapshots = data.get("snapshots") or []
        total_seen += len(snapshots)
        total_inserted += insert_snapshots(con, target["ticker"], snapshots)

        pagination = data.get("pagination") or {}
        if not snapshots:
            final_status = "empty"
            break
        if not pagination.get("has_more") or not pagination.get("pagination_key"):
            final_status = "complete"
            pagination_key = None
            break
        pagination_key = pagination.get("pagination_key")
        final_status = "partial"
        con.commit()
        time.sleep(sleep_seconds)
    else:
        final_status = "partial"
        final_error = f"hit max_pages={max_pages}"

    if total_seen > 0 and final_status in {"empty", "rate_limited"}:
        final_status = "partial" if final_status == "rate_limited" else "complete"

    update_target(
        con,
        target_id=target["target_id"],
        ticker=target["ticker"],
        start_ms=int(target["start_time_ms"]),
        end_ms=int(target["end_time_ms"]),
        status=final_status,
        seen_count=total_seen,
        error=final_error,
        pagination_key=pagination_key,
    )
    con.execute(
        """
        insert into targeted_depth_events (created_at, target_id, ticker, event, detail)
        values (?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            target["target_id"],
            target["ticker"],
            final_status,
            json.dumps(
                {"seen": total_seen, "inserted": total_inserted, "error": final_error},
                separators=(",", ":"),
            ),
        ),
    )
    con.commit()
    return final_status, total_seen, final_error


def print_summary(con: sqlite3.Connection) -> None:
    print("\nTargeted depth summary", flush=True)
    for row in con.execute(
        """
        select status, count(*) targets, count(distinct ticker) tickers, sum(snapshot_count) snapshots
        from targeted_depth_targets
        group by status
        order by status
        """
    ):
        print(
            f"  {row[0]}: targets={row[1]} tickers={row[2]} snapshots={row[3] or 0}",
            flush=True,
        )
    total = con.execute("select count(*) from orderbook_snapshots").fetchone()[0]
    print(f"  stored_orderbook_rows={total}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--out-db", required=True)
    parser.add_argument("--key-file")
    parser.add_argument("--intervals", default="12M,10M,8M,7M")
    parser.add_argument("--assets", help="Comma-separated asset filter")
    parser.add_argument("--ask-max", type=float, default=80.0)
    parser.add_argument("--pre-seconds", type=int, default=60)
    parser.add_argument("--post-seconds", type=int, default=0)
    parser.add_argument("--include-unresolved", action="store_true")
    parser.add_argument("--target-limit", type=int)
    parser.add_argument("--page-limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--sleep-seconds", type=float, default=1.2)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--newest-first", action="store_true", default=True)
    parser.add_argument("--oldest-first", action="store_false", dest="newest_first")
    parser.add_argument("--retry-empty", action="store_true")
    parser.add_argument("--retry-partial", action="store_true")
    parser.add_argument("--seed-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db)
    out_db = Path(args.out_db)
    intervals = {part.strip() for part in args.intervals.split(",") if part.strip()}
    assets = {part.strip().upper() for part in args.assets.split(",") if part.strip()} if args.assets else None

    con = connect_out(out_db)
    targets = load_targets(
        source_db=source_db,
        intervals=intervals,
        assets=assets,
        pre_seconds=args.pre_seconds,
        post_seconds=args.post_seconds,
        ask_max=args.ask_max,
        include_unresolved=args.include_unresolved,
    )
    seed_targets(con, targets)
    print(f"Seeded/known targeted checkpoints: {len(targets)}", flush=True)
    if args.seed_only:
        print_summary(con)
        return 0

    api_key = read_api_key(args.key_file)
    todo = pending_targets(
        con,
        limit=args.target_limit,
        retry_empty=args.retry_empty,
        retry_partial=args.retry_partial,
        newest_first=args.newest_first,
    )
    print(f"Targeted checkpoints this run: {len(todo)}", flush=True)
    for index, target in enumerate(todo, 1):
        captured_et = dt.datetime.fromtimestamp(
            float(target["captured_at"]),
            dt.timezone.utc,
        ).astimezone(dt.timezone(dt.timedelta(hours=-4)))
        print(
            f"[{index}/{len(todo)}] {target['ticker']} {target['interval']} {target['asset']} "
            f"capture_et={captured_et:%Y-%m-%d %H:%M:%S} ask={target['yes_ask_cents']}",
            flush=True,
        )
        status, seen, error = fetch_target(
            con=con,
            target=target,
            api_key=api_key,
            page_limit=args.page_limit,
            max_pages=args.max_pages,
            timeout=args.timeout,
            sleep_seconds=args.sleep_seconds,
        )
        print(f"  -> {status} snapshots={seen}" + (f" error={error}" if error else ""), flush=True)
        time.sleep(args.sleep_seconds)
    print_summary(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
