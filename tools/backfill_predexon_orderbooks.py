#!/usr/bin/env python3
"""Backfill Predexon Kalshi historical orderbook snapshots.

This script is intentionally standalone: it uses only the Python standard
library so it can run from the local research workspace or Replit.
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
    if len(lines) >= 2:
        return lines[1]
    return lines[0]


def connect_out(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    con.execute(
        """
        create table if not exists backfill_targets (
            ticker text primary key,
            asset text,
            window_key integer,
            close_time real,
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
    existing_cols = {
        row[1]
        for row in con.execute("pragma table_info(backfill_targets)").fetchall()
    }
    if "pagination_key" not in existing_cols:
        con.execute("alter table backfill_targets add column pagination_key text")
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
        create table if not exists backfill_events (
            id integer primary key autoincrement,
            created_at text not null,
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
    include_unresolved: bool,
    only_assets: set[str] | None,
    lookback_minutes: int,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(source_db)
    con.row_factory = sqlite3.Row
    where = ["close_time is not null", "ticker is not null"]
    if not include_unresolved:
        where.append("official_result is not null")
        where.append("official_result != ''")
    if only_assets:
        placeholders = ",".join("?" for _ in only_assets)
        where.append(f"asset in ({placeholders})")
        args: list[Any] = sorted(only_assets)
    else:
        args = []

    rows = con.execute(
        f"""
        select
            ticker,
            asset,
            min(window_key) as window_key,
            min(close_time) as close_time
        from interval_captures
        where {" and ".join(where)}
        group by ticker, asset
        order by close_time asc, asset asc
        """,
        args,
    ).fetchall()
    con.close()

    targets: list[dict[str, Any]] = []
    lookback_seconds = lookback_minutes * 60
    for row in rows:
        close_time = float(row["close_time"])
        start_time = close_time - lookback_seconds
        targets.append(
            {
                "ticker": row["ticker"],
                "asset": row["asset"],
                "window_key": row["window_key"],
                "close_time": close_time,
                "start_time_ms": int(start_time * 1000),
                "end_time_ms": int(close_time * 1000),
            }
        )
    return targets


def seed_targets(con: sqlite3.Connection, targets: list[dict[str, Any]]) -> None:
    con.executemany(
        """
        insert or ignore into backfill_targets (
            ticker, asset, window_key, close_time, start_time_ms, end_time_ms, updated_at
        ) values (
            :ticker, :asset, :window_key, :close_time, :start_time_ms, :end_time_ms, :updated_at
        )
        """,
        [{**target, "updated_at": utc_now_iso()} for target in targets],
    )
    con.commit()


def pending_targets(
    con: sqlite3.Connection,
    limit: int | None,
    retry_empty: bool,
    retry_partial: bool,
    only_assets: set[str] | None,
    newest_first: bool,
) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    statuses = ("pending", "rate_limited", "error")
    if retry_empty:
        statuses = statuses + ("empty",)
    if retry_partial:
        statuses = statuses + ("partial",)
    placeholders = ",".join("?" for _ in statuses)
    filters = [f"status in ({placeholders})"]
    args: list[Any] = list(statuses)
    if only_assets:
        asset_placeholders = ",".join("?" for _ in only_assets)
        filters.append(f"asset in ({asset_placeholders})")
        args.extend(sorted(only_assets))
    close_sort = "close_time desc" if newest_first else "close_time asc"
    sql = f"""
        select * from backfill_targets
        where {" and ".join(filters)}
        order by
            case status
                when 'pending' then 0
                when 'rate_limited' then 1
                when 'error' then 2
                else 3
            end,
            {close_sort},
            asset asc
    """
    if limit:
        sql += " limit ?"
        args.append(limit)
    return con.execute(sql, args).fetchall()


def request_json(url: str, api_key: str, timeout: int) -> tuple[int, dict[str, Any] | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tez-predexon-backfill/1.0",
            "x-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body), None
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", errors="replace")
        return exc.code, None, body
    except Exception as exc:  # noqa: BLE001 - keep the backfill resumable.
        return 0, None, f"{type(exc).__name__}: {exc}"


def build_url(ticker: str, start_ms: int, end_ms: int, limit: int, pagination_key: str | None) -> str:
    params: dict[str, Any] = {
        "ticker": ticker,
        "start_time": start_ms,
        "end_time": end_ms,
        "limit": limit,
    }
    if pagination_key:
        params["pagination_key"] = pagination_key
    return PREDEXON_ORDERBOOKS_URL + "?" + urllib.parse.urlencode(params)


def insert_snapshots(con: sqlite3.Connection, ticker: str, snapshots: list[dict[str, Any]]) -> int:
    inserted = 0
    now = utc_now_iso()
    for snap in snapshots:
        ts_ms = snap.get("timestamp")
        if ts_ms is None:
            continue
        try:
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
        except sqlite3.IntegrityError:
            continue
    return inserted


def update_target(
    con: sqlite3.Connection,
    ticker: str,
    status: str,
    snapshot_count: int,
    last_error: str | None,
    pagination_key: str | None,
) -> None:
    row = con.execute(
        """
        select min(ts_ms), max(ts_ms), count(*)
        from orderbook_snapshots
        where ticker = ?
        """,
        (ticker,),
    ).fetchone()
    first_ms, last_ms, stored_count = row
    con.execute(
        """
        update backfill_targets
        set status = ?,
            attempts = attempts + 1,
            snapshot_count = ?,
            first_snapshot_ms = ?,
            last_snapshot_ms = ?,
            pagination_key = ?,
            last_error = ?,
            updated_at = ?
        where ticker = ?
        """,
        (
            status,
            max(snapshot_count, int(stored_count or 0)),
            first_ms,
            last_ms,
            pagination_key if status in {"partial", "rate_limited"} else None,
            last_error,
            utc_now_iso(),
            ticker,
        ),
    )


def fetch_ticker(
    con: sqlite3.Connection,
    target: sqlite3.Row,
    api_key: str,
    page_limit: int,
    max_pages: int,
    timeout: int,
    sleep_seconds: float,
) -> tuple[str, int, str | None]:
    ticker = target["ticker"]
    pagination_key = target["pagination_key"] if "pagination_key" in target.keys() else None
    total_inserted = 0
    total_seen = 0
    final_status = "empty"
    final_error = None

    for page in range(max_pages):
        url = build_url(
            ticker=ticker,
            start_ms=int(target["start_time_ms"]),
            end_ms=int(target["end_time_ms"]),
            limit=page_limit,
            pagination_key=pagination_key,
        )
        status_code, data, error_body = request_json(url, api_key, timeout)
        if status_code == 429:
            final_status = "rate_limited"
            final_error = "HTTP 429 Too Many Requests"
            break
        if status_code < 200 or status_code >= 300 or data is None:
            final_status = "error"
            final_error = f"HTTP {status_code}: {error_body or 'no body'}"
            break

        snapshots = data.get("snapshots") or []
        total_seen += len(snapshots)
        total_inserted += insert_snapshots(con, ticker, snapshots)

        pagination = data.get("pagination") or {}
        if not snapshots:
            final_status = "empty"
            break
        if not pagination.get("has_more") or not pagination.get("pagination_key"):
            final_status = "complete"
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
    update_target(con, ticker, final_status, total_seen, final_error, pagination_key)
    con.execute(
        """
        insert into backfill_events (created_at, ticker, event, detail)
        values (?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            ticker,
            final_status,
            json.dumps(
                {
                    "seen": total_seen,
                    "inserted": total_inserted,
                    "error": final_error,
                },
                separators=(",", ":"),
            ),
        ),
    )
    con.commit()
    return final_status, total_seen, final_error


def print_summary(con: sqlite3.Connection) -> None:
    print("\nBackfill summary", flush=True)
    for row in con.execute(
        """
        select status, count(*) as tickers, sum(snapshot_count) as snapshots
        from backfill_targets
        group by status
        order by status
        """
    ):
        print(f"  {row[0]}: tickers={row[1]} snapshots={row[2] or 0}", flush=True)
    total = con.execute("select count(*) from orderbook_snapshots").fetchone()[0]
    print(f"  stored_snapshot_rows={total}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, help="Path to q15_interval_research_v1.sqlite3")
    parser.add_argument("--out-db", required=True, help="SQLite file to write Predexon snapshots into")
    parser.add_argument("--key-file", help="Optional key file; second non-empty line is used when present")
    parser.add_argument("--include-unresolved", action="store_true", help="Include unresolved interval tickers")
    parser.add_argument("--assets", help="Comma-separated asset filter, e.g. HYPE,BTC")
    parser.add_argument("--lookback-minutes", type=int, default=15)
    parser.add_argument("--target-limit", type=int, help="Only process this many pending targets")
    parser.add_argument("--newest-first", action="store_true", help="Fetch newest close times first")
    parser.add_argument("--page-limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--sleep-seconds", type=float, default=8.0)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retry-empty", action="store_true", help="Retry targets previously marked empty")
    parser.add_argument("--retry-partial", action="store_true", help="Retry targets previously stopped at max_pages")
    parser.add_argument("--seed-only", action="store_true", help="Create/seed target rows without fetching")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db)
    out_db = Path(args.out_db)
    only_assets = {part.strip().upper() for part in args.assets.split(",") if part.strip()} if args.assets else None

    if not source_db.exists():
        raise SystemExit(f"Source DB not found: {source_db}")

    con = connect_out(out_db)
    targets = load_targets(
        source_db=source_db,
        include_unresolved=args.include_unresolved,
        only_assets=only_assets,
        lookback_minutes=args.lookback_minutes,
    )
    seed_targets(con, targets)
    print(f"Seeded/known targets: {len(targets)}", flush=True)
    if args.seed_only:
        print_summary(con)
        return 0

    api_key = read_api_key(args.key_file)
    todo = pending_targets(
        con,
        args.target_limit,
        args.retry_empty,
        args.retry_partial,
        only_assets,
        args.newest_first,
    )
    print(f"Pending targets this run: {len(todo)}", flush=True)

    for index, target in enumerate(todo, 1):
        close_et = dt.datetime.fromtimestamp(
            float(target["close_time"]),
            dt.timezone.utc,
        ).astimezone(dt.timezone(dt.timedelta(hours=-4)))
        print(
            f"[{index}/{len(todo)}] {target['ticker']} {target['asset']} "
            f"close_et={close_et:%Y-%m-%d %H:%M:%S}",
            flush=True,
        )
        status, seen, err = fetch_ticker(
            con=con,
            target=target,
            api_key=api_key,
            page_limit=args.page_limit,
            max_pages=args.max_pages,
            timeout=args.timeout,
            sleep_seconds=args.sleep_seconds,
        )
        print(f"  -> {status} snapshots={seen}" + (f" error={err}" if err else ""), flush=True)
        time.sleep(args.sleep_seconds)

    print_summary(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
