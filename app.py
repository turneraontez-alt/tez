import os
import sys
import shutil

# --- interpreter guard (bootstrap only; NOT trading logic) ---
# This app requires Python 3.11. Only 3.11 has a working psycopg2 and the
# cffi/cryptography backend needed to sign live Kalshi orders. A bare `python3`
# can resolve to 3.12 (missing _cffi_backend + psycopg2._psycopg), which boots
# the app in a degraded, cannot-sign state while still serving HTTP 200s. If we
# were launched on anything other than 3.11, re-exec under python3.11. This guard
# FAILS CLOSED: if 3.11 cannot be reached we abort rather than run degraded, so a
# live-money process never starts unable to sign.
_safe_non311_dryrun = all(
    os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
    for name in (
        "Q15_EXEC_DRY_RUN",
        "Q15_EXEC_KILL",
        "Q15_EXEC_YES_DRY_RUN",
        "Q15_EXEC_YES_KILL",
    )
)
if sys.version_info[:2] != (3, 11) and not _safe_non311_dryrun:
    if os.environ.get("Q15_INTERP_REEXEC") == "1":
        sys.stderr.write(
            "FATAL: interpreter guard re-exec did not land on Python 3.11; "
            "refusing to start to avoid a degraded (cannot-sign) boot.\n")
        raise SystemExit(70)
    _py311 = shutil.which("python3.11")
    if not _py311:
        sys.stderr.write(
            "FATAL: Python 3.11 is required but 'python3.11' was not found on "
            "PATH; refusing to start to avoid a degraded (cannot-sign) boot.\n")
        raise SystemExit(70)
    os.environ["Q15_INTERP_REEXEC"] = "1"
    try:
        os.execv(_py311, [_py311, *sys.argv])
    except OSError as _exc:
        sys.stderr.write(f"FATAL: failed to re-exec under {_py311}: {_exc}\n")
        raise SystemExit(70)
elif sys.version_info[:2] != (3, 11):
    sys.stderr.write(
        "WARNING: running on Python "
        f"{sys.version_info.major}.{sys.version_info.minor} for local dry-run only; "
        "live trading remains blocked by kill switches.\n"
    )
# --- end interpreter guard ---

import atexit
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify

from q15_upgrade.kalshi_rest import KalshiClient
from market_cache import MarketResultCache
from spot_client import get_spot
import cycle_watchdog
import startup_config_manifest
import version as build_version
from q15_upgrade.orderbook import parse_orderbook, OrderbookTracker
from analysis import AssetEngine
from notifications.alert_config import AlertConfig
from db import SignalStore
from notifications.notifier import TelegramNotifier
from notifications.outbox_v9 import ReliableTelegramOutbox  # Q15_V9_RELIABLE_ALERTS
from q15_upgrade.oos_v9 import OutOfSampleEvaluator  # Q15_V9_OOS
from q15_upgrade.signals import SignalEngine
from performance import PerformanceTracker
from q15_upgrade.learning import LearningEngine
from scalp import ScalpEngine
from notifications.reporting import HourlyReporter
from q15_upgrade.runtime import Q15Runtime, attach_orderbook_levels
from q15_upgrade.store_patch import patch_store
from q15_upgrade.hybrid_data import HybridMarketData
from q15_upgrade.ws_client import get_feed as get_ws_feed
from q15_upgrade.window_focus import TwoWindowFocusManager  # Q15_TWO_WINDOW_APP_INTEGRATION
from q15_upgrade.calibrated_edge import CalibratedEdgeEngine
from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95  # Q15_V952_RUNTIME_BINDING
from q15_upgrade.professional_v7 import ProfessionalV7Engine  # Q15_V7_PROFESSIONAL_LIVENESS

from q15_upgrade.v5_hardening import (
    apply_snapshot_freshness,
    enforce_fail_closed,
    risk_preview,
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Stamp the running build at boot so the Repl console shows exactly which code
# this process loaded (Stop ▸ Run is required to pick up a synced update).
_bi = build_version.version_payload()
logger.info("BUILD %s (%s) shipped=%s — %s · tests=%s",
            _bi.get("running_commit") or _bi.get("commit"), _bi.get("branch"), _bi.get("committed_at"),
            f"{_bi.get('summary')} stale={_bi.get('build_info_stale')}", _bi.get("tests"))





STARTUP_CONFIG_MANIFEST = startup_config_manifest.check_startup_config(
    send_alert=os.environ.get("Q15_AUTOSTART_REFRESH", "1") != "0"
)

# Asset -> series ticker mapping for 15-min markets
SERIES_MAP = {
    "BTC":  "KXBTC15M",
    "ETH":  "KXETH15M",
    "SOL":  "KXSOL15M",
    "XRP":  "KXXRP15M",
    "DOGE": "KXDOGE15M",
    "BNB":  "KXBNB15M",
    "HYPE": "KXHYPE15M",
}
ASSETS = list(SERIES_MAP.keys())

REFRESH_INTERVAL = 1.0
DISCOVERY_INTERVAL = 30
DETAIL_INTERVAL = 5  # refresh market detail (volume) every N seconds
FETCH_DEADLINE = 3.0  # max seconds a cycle waits on concurrent fetches
# A fetch still running after this long is treated as a permanently hung upstream
# and dropped from in-flight tracking so the dict can't grow without bound over
# the life of the loop. Generous vs FETCH_DEADLINE: only abandons true hangs.
FETCH_INFLIGHT_TTL = float(os.environ.get("Q15_FETCH_INFLIGHT_TTL_S") or 60.0)

client = KalshiClient()
# Shared cache of immutable settled-market results. All settlement reconcilers
# go through this so a resolved market is fetched from Kalshi once, not ~4x.
market_cache = MarketResultCache(client)
engines = {a: AssetEngine(a) for a in ASSETS}
# Q15_V5_APP_HARDENING: parser and tracker now use the same schema.
for _engine in engines.values():
    _engine.ob_tracker = OrderbookTracker()
state = {}
state_lock = threading.Lock()
# Per-asset engine update epoch captured at snapshot-publish time, under
# state_lock, so /api/health computes data_age from a value consistent with the
# state it just read (instead of an unsynchronized read of engines[a]).
engine_update_ts = {}
_last_detail = {a: 0 for a in ASSETS}

# Entry-alert subsystem (read-only; never places orders).
config = AlertConfig()
ws_feed = get_ws_feed() if config.ws_enabled else None
market_data = HybridMarketData(client, ws_feed)
store = patch_store(SignalStore())
raw_notifier = TelegramNotifier()
telegram_outbox = ReliableTelegramOutbox(store, raw_notifier)
notifier = telegram_outbox
learner = LearningEngine(store, config, notifier=notifier)  # Q15_V4_LEARNING_INTEGRATION
upgrade = Q15Runtime(config, learner)
logger.info("Q15 settings active: %s", asdict(upgrade.settings))
signal_engine = SignalEngine(config, store, notifier, learner)
perf = PerformanceTracker(store, market_cache)
scalp_engine = ScalpEngine(store, notifier, config)
scalp_engine.learning_v2 = learner
reporter = HourlyReporter(store, notifier, config, perf, learner, scalp_engine)

focus_manager = TwoWindowFocusManager(store, notifier, config, market_cache)

calibrated_edge = CalibratedEdgeEngine(store, notifier, config, learner)


checkpoint_v95 = CheckpointPolicyV95(store)
# Give the hourly reporter the V9.5 ledger so it can publish the interval
# (15M/10M/7M) and pick-rank (#1/#2/#3) track record.
reporter.v95_ledger = checkpoint_v95.ledger
# Let the V9.5 ledger settle predictions directly from official Kalshi results,
# not only via the signals table, so every prediction gets graded.
checkpoint_v95.kalshi_client = market_cache
professional_v7 = ProfessionalV7Engine(store, notifier, config, learner)

oos_v9 = OutOfSampleEvaluator(store)
SERVER_STARTED_AT = time.time()
SERVER_STARTED_AT_ISO = datetime.now(timezone.utc).isoformat()

_last_cycle_ok = None
_last_learn = 0.0


def _safe(stage, fn, *a):
    try:
        fn(*a)
    except Exception as e:
        logger.exception("%s failed", stage)


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _now_iso_z():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_remaining(close_time_str):
    if not close_time_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return max(0, int((close_dt - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        # A malformed close_time still returns None (callers handle it), but log
        # it so a bad/changed upstream timestamp format is visible instead of
        # silently disabling every checkpoint clock.
        logger.warning("Could not parse close_time %r", close_time_str)
        return None


def _close_time_epoch(close_time_str):
    if not close_time_str:
        return None
    try:
        return datetime.fromisoformat(close_time_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        logger.warning("Could not parse close_time epoch %r", close_time_str)
        return None


def _observe_market_activity(asset, ticker, market, ob_delta, now):
    try:
        from market_activity import book_changed, get_market_activity_recorder

        get_market_activity_recorder().observe(
            asset=asset,
            ticker=ticker,
            market=market,
            orderbook_changed=book_changed(ob_delta),
            now=now,
        )
    except Exception:
        logger.debug("market activity observe skipped", exc_info=True)


def _observe_ladder_probe(asset, close_time_epoch, seconds_remaining, now):
    try:
        from ladder_probe import get_ladder_probe

        get_ladder_probe().observe(
            asset=asset,
            series_ticker=SERIES_MAP[asset],
            close_time=close_time_epoch,
            seconds_remaining=seconds_remaining,
            now=now,
        )
    except Exception:
        logger.debug("ladder probe observe skipped", exc_info=True)


def _observe_path_recorder(asset, snap, close_time_epoch, now):
    try:
        from path_recorder import get_path_recorder
        from settlement_index import settlement_index_context

        index_ctx = settlement_index_context(
            asset,
            spot_px=snap.get("underlying_current"),
            now=now,
        )
        get_path_recorder().observe(
            asset=asset,
            close_time=close_time_epoch,
            seconds_remaining=snap.get("seconds_remaining"),
            index_px=index_ctx.get("index_px"),
            spot_px=snap.get("underlying_current"),
            yes_bid=snap.get("yes_bid"),
            yes_ask=snap.get("yes_ask"),
            now=now,
        )
    except Exception:
        logger.debug("path recorder observe skipped", exc_info=True)


def _observe_strangle_shadow(asset, snap, close_time_epoch, now):
    try:
        from strangle_shadow import get_strangle_shadow

        get_strangle_shadow().observe(
            asset=asset,
            close_time=close_time_epoch,
            seconds_remaining=snap.get("seconds_remaining"),
            yes_bid=snap.get("yes_bid"),
            yes_ask=snap.get("yes_ask"),
            now=now,
        )
    except Exception:
        logger.debug("strangle shadow observe skipped", exc_info=True)


def _feed_watchdog_age(status, age_key, now):
    age = status.get(age_key)
    if age is not None:
        return age
    if status.get("enabled") and status.get("status") != "disabled":
        return max(0.0, now - SERVER_STARTED_AT)
    return None


def _flush_path_recorder(asset, close_time_str, now):
    try:
        from path_recorder import get_path_recorder

        get_path_recorder().flush_window(
            asset=asset,
            close_time=_close_time_epoch(close_time_str),
            now=now,
        )
    except Exception:
        logger.debug("path recorder flush skipped", exc_info=True)


def _flush_expired_path_records(now):
    try:
        from path_recorder import get_path_recorder

        get_path_recorder().flush_expired(now=now)
    except Exception:
        logger.debug("expired path recorder flush skipped", exc_info=True)
    try:
        from strangle_shadow import get_strangle_shadow

        get_strangle_shadow().finalize_expired(now=now)
    except Exception:
        logger.debug("strangle shadow finalize skipped", exc_info=True)


def discover_single(asset):
    """Return the soonest future market for an asset (live or upcoming).

    15-min crypto markets aren't queryable by status=open, so we query a
    forward close-time window and take the soonest market that hasn't closed.
    The market may be live (floor_strike set) or upcoming (strike not set).
    """
    now = int(time.time())
    markets = client.discover(SERIES_MAP[asset], min_close_ts=now,
                              max_close_ts=now + 6 * 3600)
    if not markets:
        markets = client.discover(SERIES_MAP[asset])
    now_iso = _now_iso_z()
    future = [m for m in markets if m.get("close_time", "") > now_iso]
    if not future:
        return None
    future.sort(key=lambda m: m.get("close_time", ""))
    return future[0]


def is_live_market(market):
    """A market is live/tradeable once Kalshi assigns its floor_strike."""
    return market.get("floor_strike") is not None


def market_has_expired(market):
    close_time_str = market.get("close_time", "")
    if not close_time_str:
        return False
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= close_dt
    except Exception:
        return False


def _parse_volume(market):
    v = market.get("volume_fp") or market.get("volume", 0)
    try:
        return round(float(v))
    except Exception:
        return 0


def _normalize_book_source(value):
    raw = str(value or "").strip().lower()
    if raw in {"ws", "websocket"}:
        return "ws"
    if raw in {"rest", "rest-fallback"}:
        return "rest"
    if raw in {"rest-depth-retry", "depth-retry"}:
        return "rest-depth-retry"
    return None


def _book_depth_status(parsed_orderbook):
    if not isinstance(parsed_orderbook, dict):
        return "missing", "parse_failed"
    qty_keys = ("yes_bid_qty", "yes_ask_qty", "no_bid_qty", "no_ask_qty")
    present = [parsed_orderbook.get(k) is not None for k in qty_keys]
    if all(present):
        return "ok", None
    if any(present):
        return "partial", "partial_side_depth"
    has_price = any(parsed_orderbook.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))
    return "missing", "price_without_depth" if has_price else "empty_orderbook"


def _raw_book_depth_status(raw_orderbook):
    try:
        return _book_depth_status(parse_orderbook(raw_orderbook))
    except Exception:
        return "missing", "parse_failed"


def _with_orderbook_meta(book, *, source, updated_at=None):
    if not isinstance(book, dict):
        return book
    out = dict(book)
    out["_hybrid_source"] = source
    if updated_at is not None:
        out["_updated_at"] = updated_at
    elif out.get("_updated_at") is None:
        out["_updated_at"] = time.time()
    return out


def _attach_spot_depth_context(snapshot, asset):
    """Attach actual-coin spot depth, plus an explicit missing reason.

    The collector is best-effort public market data. A blank spot-depth field is
    useful only if we know whether it was disabled, still warming up, stale, or
    errored, so stamp status on every live snapshot.
    """
    try:
        from spot_depth import get_latest_spot_depth, spot_depth_health

        spot_depth = get_latest_spot_depth(asset)
        if spot_depth is not None:
            snapshot["spot_depth"] = spot_depth
            snapshot["spot_depth_status"] = "ok"
            snapshot["spot_depth_missing_reason"] = None
            return snapshot

        health = spot_depth_health()
        snapshot["spot_depth_status"] = "missing"
        if not health.get("enabled"):
            reason = "collector_disabled"
        elif not health.get("have_ws"):
            reason = "websocket_dependency_missing"
        elif asset not in set(health.get("assets") or []):
            reason = "asset_not_configured"
        else:
            book_ages = health.get("book_age_seconds") or {}
            if asset not in book_ages:
                reason = "no_spot_book_yet"
            else:
                reason = "spot_book_stale_or_not_recorded"
        snapshot["spot_depth_missing_reason"] = reason
    except Exception as exc:  # noqa: BLE001 - context only; never break alerts
        snapshot["spot_depth_status"] = "error"
        snapshot["spot_depth_missing_reason"] = f"{type(exc).__name__}: {exc}"[:200]
        logger.debug("spot depth attach failed for %s", asset, exc_info=True)
    return snapshot


def fetch_asset_raw(asset, market, now, last_trade_ts):
    """Network worker that preserves source timestamps for freshness gates.

    Only the freshness-critical, websocket-backed legs (orderbook, trades, spot)
    run here, so this stays well under the fetch deadline. Market detail (volume)
    is a slow Kalshi REST call and is refreshed OFF this critical path — see
    fetch_market_detail and the detail cache in refresh_loop — so it can never
    blow the deadline and age the snapshot.
    """
    started = time.time()
    ticker = market["ticker"]
    min_ts = int(last_trade_ts - 1) if last_trade_ts else int(now - 60)
    ob_raw = market_data.get_orderbook(ticker)
    initial_depth_status, initial_depth_reason = _raw_book_depth_status(ob_raw)
    depth_retry_used = False
    depth_retry_reason = None
    if initial_depth_status == "missing":
        # WebSocket/orderbook snapshots can occasionally contain prices without
        # usable size, or no levels at all. Depth is critical for later research,
        # so do one direct REST retry before publishing a depth-blank snapshot.
        try:
            rest_book = market_data.rest.get_orderbook(ticker)
        except Exception as exc:  # noqa: BLE001 - diagnostics only; keep cycle alive
            rest_book = None
            depth_retry_reason = f"rest_retry_failed:{type(exc).__name__}"
        rest_status, rest_reason = _raw_book_depth_status(rest_book)
        if rest_status in {"ok", "partial"}:
            ob_raw = _with_orderbook_meta(
                rest_book, source="rest-depth-retry", updated_at=time.time()
            )
            depth_retry_used = True
            initial_depth_status, initial_depth_reason = rest_status, rest_reason
        elif depth_retry_reason is None:
            depth_retry_reason = rest_reason or "rest_retry_no_depth"
    trades = market_data.get_trades(ticker, min_ts=min_ts)
    spot = get_spot(asset)
    completed = time.time()

    latest_trade_event_ts = None
    for trade in trades or []:
        try:
            trade_ts = float(trade.get("ts"))
        except (TypeError, ValueError):
            continue
        latest_trade_event_ts = (
            trade_ts
            if latest_trade_event_ts is None
            else max(latest_trade_event_ts, trade_ts)
        )

    book_event_ts = None
    book_source = None
    if isinstance(ob_raw, dict):
        book_source = _normalize_book_source(
            ob_raw.get("_hybrid_source") or ob_raw.get("_source") or ob_raw.get("source")
        )
        for key in ("_updated_at", "updated_at", "event_ts", "ts", "timestamp"):
            try:
                value = ob_raw.get(key)
                if value is not None:
                    book_event_ts = float(value)
                    break
            except (TypeError, ValueError):
                pass
    if book_source is None:
        book_source = "rest" if not market_data.is_connected() else "unknown"

    return {
        "asset": asset,
        "ticker": ticker,
        "ob_raw": ob_raw,
        "trades": trades,
        "spot": spot,
        "source": book_source,
        "book_source": book_source,
        "fetch_started_at": started,
        "fetch_completed_at": completed,
        "fetch_latency_seconds": round(completed - started, 4),
        "latest_trade_event_ts": latest_trade_event_ts,
        "book_event_ts": book_event_ts,
        "kalshi_depth_status": initial_depth_status,
        "kalshi_depth_missing_reason": initial_depth_reason or depth_retry_reason,
        "kalshi_depth_retry_used": depth_retry_used,
    }


def fetch_market_detail(ticker):
    """Slow Kalshi REST market detail (volume), fetched off the critical path."""
    return client.get_market(ticker)


def _fetch_result_is_current(active, asset, result):
    """True if a fetch result still belongs to the asset's currently-active market.

    A request submitted before a market rolled over can land a cycle later with
    the *prior* ticker. Ingesting it would reset the engine to a stale ticker, so
    the loop drops any result whose asset is no longer active or whose ticker no
    longer matches the active market.
    """
    market = active.get(asset)
    return bool(market) and market.get("ticker") == result.get("ticker")


def _resolve_cached_detail(detail_cache, asset, active_ticker):
    """Last-good market detail (volume) for ``asset`` — only if it matches the
    active ticker. Detail is cached per ticker so a rollover never reuses the
    prior market's volume; on a ticker mismatch this returns None and the loop
    falls back to the base market (volume 0) until fresh detail arrives.
    """
    cached = detail_cache.get(asset)
    return cached[1] if (cached and cached[0] == active_ticker) else None


def _harvest_and_submit(executor, inflight, active, now, deadline, submit_fn, stale_ttl=None):
    """Drive the per-asset concurrent fetch with at-most-one request in flight.

    Harvests any previous-cycle requests that have finished, submits a fresh
    request for each active asset not already in flight, then waits up to
    ``deadline`` for *this* cycle's requests. A request that is still running at
    the deadline stays in ``inflight`` and is harvested in a later cycle, so one
    slow upstream cannot freeze the whole dashboard. A request still running after
    ``stale_ttl`` seconds is treated as a permanently hung upstream: it is
    cancelled best-effort and dropped from tracking so ``inflight`` can't grow
    without bound (and a fresh request can take its place). Returns
    ``{asset: result}`` for everything that completed; ``inflight`` is mutated in
    place, mapping ``asset -> (Future, submitted_at)``.
    """
    if stale_ttl is None:
        stale_ttl = FETCH_INFLIGHT_TTL
    results = {}
    # 1) harvest any previous-cycle requests that have since finished; abandon any
    #    that have been hung past the TTL.
    for a in list(inflight.keys()):
        f, submitted_at = inflight[a]
        if f.done():
            try:
                results[a] = f.result()
            except Exception as e:
                logger.warning(f"fetch {a}: {e}")
            del inflight[a]
        elif now - submitted_at > stale_ttl:
            f.cancel()  # best-effort; a running fetch can't be interrupted
            logger.warning(f"fetch {a}: abandoned after {now - submitted_at:.0f}s in flight")
            del inflight[a]
    # 2) submit a fresh request only for active assets not already in flight
    futs = {}
    for asset, market in active.items():
        if asset in inflight:
            continue
        f = executor.submit(submit_fn, asset, market, now)
        inflight[asset] = (f, now)
        futs[f] = asset
    # 3) wait up to the per-cycle deadline for THIS cycle's requests; slow ones
    #    stay in flight and are harvested in a later cycle.
    try:
        for fut in as_completed(list(futs.keys()), timeout=deadline):
            a = futs[fut]
            try:
                results[a] = fut.result()
            except Exception as e:
                logger.warning(f"fetch {a}: {e}")
            finally:
                # Remove successful futures too; otherwise stale results repeat.
                inflight.pop(a, None)
    except TimeoutError:
        slow = [futs[f] for f in futs if futs[f] in inflight]
        logger.warning(f"fetch deadline {deadline}s exceeded; deferring {slow}")
    return results


def refresh_loop(max_cycles=None):
    """The ~1s cycle that drives every subsystem.

    Runs forever in production. ``max_cycles`` bounds the number of iterations so
    the loop can be driven deterministically from tests (default ``None`` keeps
    the production infinite behavior unchanged).
    """
    last_discovery = 0
    current_markets = {}
    cycling = {}
    inflight = {}  # asset -> Future (at most one outstanding fetch per asset)
    detail_inflight = {}  # asset -> (ticker, Future) for the off-critical detail fetch
    detail_cache = {}     # asset -> (ticker, detail) last-good market volume
    executor = ThreadPoolExecutor(max_workers=8)
    # Separate pool for off-critical market-detail (volume) fetches. Kalshi REST
    # get_market can take seconds; sharing the critical pool means a burst of slow
    # detail calls could occupy every worker and starve the freshness-critical
    # asset fetches, aging the snapshot. An isolated pool bounds that blast radius.
    detail_executor = ThreadPoolExecutor(max_workers=4)
    # Forever-loop: shut the pools down on interpreter exit so we don't leak the
    # worker threads (the bounded-test path shuts down explicitly before return).
    atexit.register(executor.shutdown, wait=False)
    atexit.register(detail_executor.shutdown, wait=False)
    if max_cycles is None:
        cycle_watchdog.write_heartbeat(status="startup")
        cycle_watchdog.start_heartbeat_supervisor()
    cycles = 0
    while True:
        cycle_clock = time.monotonic()
        cycle_start = time.time()
        now = cycle_start
        cycle_watchdog.write_heartbeat(now=now, cycle=cycles, status="cycle_start")
        ct = cycle_watchdog.CycleTimer()
        try:
            # -- discovery --
            if now - last_discovery >= DISCOVERY_INTERVAL or not current_markets:
                for asset in ASSETS:
                    try:
                        m = discover_single(asset)
                        if m:
                            current_markets[asset] = m
                            cycling.pop(asset, None)
                    except Exception as e:
                        logger.warning(f"discover {asset}: {e}")
                last_discovery = now

            # -- expiry detection --
            for asset in ASSETS:
                m = current_markets.get(asset)
                if m and market_has_expired(m):
                    _flush_path_recorder(asset, m.get("close_time", ""), now)
                    cycling.setdefault(asset, {"since": now, "last_try": 0})
                    current_markets.pop(asset, None)
                    logger.info(f"{asset} expired — cycling")
            _flush_expired_path_records(now)

            # -- re-discover cycling assets --
            for asset in list(cycling.keys()):
                cyc = cycling[asset]
                if now - cyc["last_try"] >= 3:
                    cyc["last_try"] = now
                    try:
                        nm = discover_single(asset)
                        if nm:
                            current_markets[asset] = nm
                            cycling.pop(asset, None)
                            logger.info(f"{asset} -> {nm['ticker']} (cycle complete)")
                    except Exception as e:
                        logger.warning(f"re-discover {asset}: {e}")

            # Split into live (strike assigned, tradeable) vs upcoming.
            active = {a: m for a, m in current_markets.items() if is_live_market(m)}
            upcoming = {a: m for a, m in current_markets.items() if not is_live_market(m)}
            # Subscribing is a best-effort feed hint, not part of the decision path:
            # a websocket-layer error here must never halt the cycle (that would
            # freeze the dashboard and cut off the alerts the owner trades on). The
            # REST fetch below still serves fresh data if the subscribe fails.
            try:
                market_data.subscribe([m.get("ticker") for m in active.values() if m.get("ticker")])
            except Exception as e:
                logger.warning(f"market_data.subscribe failed (continuing on REST): {e}")

            # -- concurrent fetch (network only) --
            # Per-asset in-flight tracking: at most one outstanding request per
            # asset, so a persistently slow upstream cannot pile work onto the
            # executor and starve other assets' fetches.
            def _submit_fetch(asset, market, when):
                return fetch_asset_raw(asset, market, when, engines[asset].last_trade_ts)

            results = _harvest_and_submit(
                executor, inflight, active, now, FETCH_DEADLINE, _submit_fetch
            )

            # -- decoupled market-detail (volume) refresh, off the freshness path --
            # Kalshi REST get_market can take seconds; keeping it out of the
            # critical fetch stops it from blowing the deadline and aging the
            # snapshot. Volume is not freshness-critical, so last-good detail (kept
            # per ticker so a rollover never reuses the prior market's volume) is
            # fine between refreshes. At most one detail fetch in flight per asset.
            for a in list(detail_inflight.keys()):
                tkr, dfut = detail_inflight[a]
                if dfut.done():
                    try:
                        d = dfut.result()
                        if d:
                            detail_cache[a] = (tkr, d)
                    except Exception as e:
                        logger.warning(f"detail {a}: {e}")
                    del detail_inflight[a]
            for asset, market in active.items():
                tkr = market.get("ticker")
                if not tkr or asset in detail_inflight:
                    continue
                if (now - _last_detail[asset]) >= DETAIL_INTERVAL:
                    _last_detail[asset] = now
                    detail_inflight[asset] = (tkr, detail_executor.submit(fetch_market_detail, tkr))
            # Prune last-good detail that can no longer be consumed: an entry whose
            # asset is not live this cycle, or whose cached ticker no longer matches
            # the active market (rolled over). `_resolve_cached_detail` already
            # refuses a ticker mismatch, so this is purely housekeeping — it keeps
            # detail_cache from holding a dead market's volume indefinitely.
            for a in list(detail_cache.keys()):
                m = active.get(a)
                if m is None or detail_cache[a][0] != m.get("ticker"):
                    if a not in detail_inflight:
                        del detail_cache[a]

            # -- ingest sequentially into engines --
            prelim = {}
            for a, r in results.items():
                # Drop late in-flight results whose market has rolled over or
                # is no longer active (avoids resetting an engine to a stale
                # ticker and keeps the snapshot loop's active[a] lookups safe).
                if not _fetch_result_is_current(active, a, r):
                    continue
                # Isolate ingest per asset: a poisoned tick (bad timestamp,
                # malformed orderbook) for one asset must not abort the loop and
                # starve every *other* asset of a snapshot this cycle. Skip the
                # bad asset; the rest still publish (mirrors the build_snapshot
                # loop's per-asset try/except below).
                try:
                    eng = engines[a]
                    eng.ensure_market(r["ticker"])
                    eng.ingest_trades(r["trades"], now)
                    eng.ingest_spot(r["spot"], now)
                    ob_parsed = parse_orderbook(r["ob_raw"])
                    yb, ya = ob_parsed["yes_bid"], ob_parsed["yes_ask"]
                    ob_parsed["spread"] = (ya - yb) if (yb is not None and ya is not None) else None
                    depth_status, depth_reason = _book_depth_status(ob_parsed)
                    r["kalshi_depth_status"] = depth_status
                    r["kalshi_depth_missing_reason"] = (
                        depth_reason or r.get("kalshi_depth_missing_reason")
                    )
                    ob_delta = eng.ob_tracker.update(ob_parsed)
                    prelim[a] = (r, ob_parsed, ob_delta)
                except Exception as e:
                    logger.error(f"ingest {a}: {e}")

            # -- broader market direction (BTC/ETH underlying 60s) --
            broader = {}
            for k in ("BTC", "ETH"):
                if k in engines:
                    ch = engines[k].underlying_change_pct(now, 60)
                    broader[k] = (1 if ch > 0.02 else -1 if ch < -0.02 else 0) if ch is not None else 0

            # -- build snapshots --
            for a, (r, ob_parsed, ob_delta) in prelim.items():
                eng = engines[a]
                detail = _resolve_cached_detail(detail_cache, a, active[a].get("ticker"))
                market = {**active[a], **(detail or {})}
                market["_volume"] = _parse_volume(market)
                close_epoch = _close_time_epoch(market.get("close_time", ""))
                try:
                    _observe_market_activity(a, market.get("ticker"), market, ob_delta, now)
                    snap = eng.build_snapshot(
                        a, market, ob_parsed, ob_delta, r["spot"],
                        broader, r.get("source") == "ws", now,
                    )
                    snap = attach_orderbook_levels(snap, ob_parsed, market)
                    snap["kalshi_depth_status"] = r.get("kalshi_depth_status")
                    snap["kalshi_depth_missing_reason"] = r.get("kalshi_depth_missing_reason")
                    snap["kalshi_depth_retry_used"] = bool(r.get("kalshi_depth_retry_used"))
                    snap = _attach_spot_depth_context(snap, a)
                    snap = apply_snapshot_freshness(
                        snap, r, now, market_data.health(), config
                    )
                    _observe_ladder_probe(a, close_epoch, snap.get("seconds_remaining"), now)
                    _observe_path_recorder(a, snap, close_epoch, now)
                    _observe_strangle_shadow(a, snap, close_epoch, now)
                    eng.candles.evict(now)
                    with state_lock:
                        state[a] = snap
                        engine_update_ts[a] = eng.last_update_ts
                except Exception as e:
                    logger.error(f"build_snapshot {a}: {e}")

            # -- upcoming (strike not yet assigned) states --
            for asset, m in upcoming.items():
                secs = _seconds_remaining(m.get("close_time", ""))
                # Markets open ~15 min before close, when the strike is set.
                opens_in = max(0, secs - 900) if secs is not None else None
                with state_lock:
                    state[asset] = {
                        "asset": asset,
                        "ticker": m.get("ticker"),
                        "cycling": False,
                        "market_state": "upcoming",
                        "seconds_remaining": secs,
                        "opens_in_seconds": opens_in,
                        "close_time": m.get("close_time", ""),
                        "target": "strike set at open",
                        "flags": ["Upcoming — opens ~15m before close"],
                        "last_updated": _iso_now(),
                        "opportunity_score": 0,
                        "error": None,
                    }

            # -- cycling / no-market states --
            for asset in ASSETS:
                if asset in cycling:
                    with state_lock:
                        existing = state.get(asset, {})
                        state[asset] = {
                            **existing,
                            "asset": asset,
                            "ticker": existing.get("ticker"),
                            "cycling": True,
                            "market_state": "cycling",
                            "cycling_elapsed": int(now - cycling[asset]["since"]),
                            "seconds_remaining": 0,
                            "flags": ["Cycling — finding next market"],
                            "last_updated": _iso_now(),
                            "error": None,
                            "opportunity_score": existing.get("opportunity_score", 0),
                        }
                elif asset not in active and asset not in upcoming:
                    with state_lock:
                        state[asset] = {
                            "asset": asset, "ticker": None, "cycling": False,
                            "market_state": "none",
                            "error": "No upcoming 15-min market found",
                            "last_updated": _iso_now(), "opportunity_score": 0,
                        }

            # -- entry-alert evaluation + settlement reconciliation --
            with state_lock:
                snaps = dict(state)
            ws_health = market_data.health()
            feed_ages = {}
            try:
                from coinbase_adv_l2 import coinbase_adv_l2_health
                _coinbase_l2_health = coinbase_adv_l2_health()
                feed_ages["coinbase_adv_l2"] = _coinbase_l2_health.get("snapshot_age_seconds")
            except Exception:
                logger.debug("coinbase L2 feed freshness monitor skipped", exc_info=True)
            for feed_name, age_key in (
                ("settlement_index", "latest_age_seconds"),
                ("ladder_probe", "last_capture_age_seconds"),
                ("market_activity", "latest_age_seconds"),
                ("path_recorder", "latest_point_age_seconds"),
                ("liq_feed", "latest_age_seconds"),
                ("strangle_shadow", "latest_age_seconds"),
            ):
                try:
                    if feed_name == "settlement_index":
                        from settlement_index import settlement_index_health as _health_fn
                    elif feed_name == "ladder_probe":
                        from ladder_probe import ladder_health as _health_fn
                    elif feed_name == "market_activity":
                        from market_activity import market_activity_health as _health_fn
                    elif feed_name == "path_recorder":
                        from path_recorder import path_recorder_health as _health_fn
                    elif feed_name == "liq_feed":
                        from liq_feed import liq_health as _health_fn
                    else:
                        from strangle_shadow import strangle_shadow_health as _health_fn
                    feed_ages[feed_name] = _feed_watchdog_age(_health_fn(), age_key, now)
                except Exception:
                    logger.debug("%s feed freshness monitor skipped", feed_name, exc_info=True)
            cycle_watchdog.observe_feed_ages(feed_ages, now=now)
            global _last_cycle_ok, _last_learn
            try:
                snaps = ct.time("focus_pre_enrich", focus_manager.pre_enrich, snaps, now)
                snaps = ct.time("upgrade_enrich", upgrade.enrich_all, snaps, now, ws_health)
                snaps = ct.time("learner_enrich", learner.enrich_and_observe, snaps, now, ws_health)
                snaps = {
                    asset: risk_preview(enforce_fail_closed(snap))
                    for asset, snap in snaps.items()
                }
                snaps = ct.time("calibrated_edge", calibrated_edge.preview_all, snaps, now, ws_health)
                snaps = ct.time("run_cycle", checkpoint_v95.run_cycle, snaps, now, ws_health, focus_manager, calibrated_edge, notifier)
                snaps = ct.time("professional_v7", professional_v7.observe_all, snaps, now, ws_health)
            except Exception:
                # A single enrichment stage must not freeze the dashboard for every
                # asset (the previous, now-stale snapshot would otherwise persist
                # because state.update never ran) nor skip the best-effort
                # subsystems below. Log it and publish whatever enrichment did
                # complete; the next cycle retries from a clean copy of state.
                logger.exception("Enrichment stage failed; publishing partial snapshot")
            finally:
                with state_lock:
                    state.update(snaps)
            try:
                deep_snaps = ct.time("deep_eval_snapshots", focus_manager.deep_evaluation_snapshots, snaps, signal_engine, scalp_engine)
            except Exception:
                logger.exception("deep_evaluation_snapshots failed; skipping signal/scalp this cycle")
                deep_snaps = {}
            ct.safe("signals", signal_engine.evaluate_all, deep_snaps, now, ws_health)
            ct.safe("scalp", scalp_engine.evaluate, deep_snaps, now)
            ct.safe("focus_settlement", focus_manager.reconcile_settlements, now)
            ct.safe("report", reporter.maybe_send, now)
            # Read-only challenger shadow: deliver the per-15-min accuracy
            # comparison (challenger vs current system). No-op when disabled.
            try:
                from q15_upgrade.challenger.runner import get_runner as _challenger_runner
                _cr = _challenger_runner()
                if _cr is not None:
                    _cr_msg = _cr.drain_report()
                    if _cr_msg:
                        notifier.send(_cr_msg)
            except Exception:
                logger.debug("challenger shadow report skipped", exc_info=True)
            # Read-only Polymarket up/down shadow: enqueue a settlement reconcile
            # (throttled) and deliver its compact card. No-op when disabled; all
            # network/DB work runs on the shadow's own worker, never this loop.
            try:
                from q15_upgrade.polymarket.runner import get_runner as _polymarket_runner
                _pr = _polymarket_runner()
                if _pr is not None:
                    _pr.reconcile(now)
                    _pr_msg = _pr.drain_report()
                    if _pr_msg:
                        notifier.send(_pr_msg)
            except Exception:
                logger.debug("polymarket shadow report skipped", exc_info=True)
            # Read-only Ultoim Build research overlay: enqueue a settlement
            # reconcile (throttled) against the shared Kalshi result cache. No-op
            # when disabled; all grading/DB/Telegram run on Ultoim's own worker.
            try:
                from q15_upgrade.ultoim.runner import get_runner as _ultoim_runner
                _ur = _ultoim_runner()
                if _ur is not None:
                    _ur.reconcile(now, market_cache)
            except Exception:
                logger.debug("ultoim reconcile skipped", exc_info=True)
            # Read-only Ultoim V2 paper entry-alert overlay: enqueue a settlement
            # reconcile (throttled) against the shared Kalshi result cache and emit
            # the throttled research recap. No-op when disabled (default); all
            # grading/DB/Telegram run on V2's own worker, never this loop.
            try:
                from q15_upgrade.ultoim_v2.runner import get_runner as _ultoim_v2_runner
                _u2r = _ultoim_v2_runner()
                if _u2r is not None:
                    _u2_startup_alert = _u2r.exit_warning_startup_alert_message()
                    if _u2_startup_alert:
                        notifier.send(_u2_startup_alert)
                    _u2r.reconcile(now, market_cache)
                    _u2r.maybe_send_recap(now)
            except Exception:
                logger.debug("ultoim_v2 reconcile skipped", exc_info=True)
            # High Volatility Flip: separate paper-only alert ledger/scoreboard.
            try:
                from q15_upgrade.high_vol_flip.runner import get_runner as _hvf_runner
                _hvf = _hvf_runner()
                if _hvf is not None:
                    _hvf.reconcile(now, market_cache)
            except Exception:
                logger.debug("high_vol_flip reconcile skipped", exc_info=True)
            if now - _last_learn >= 10:           # heavy DB work: every 10s, not 1s
                ct.safe("perf", perf.reconcile, now)
                ct.safe("learning_reconcile", learner.reconcile, now, market_cache)
                ct.safe("learner", learner.recompute, now)
                _last_learn = now
            _last_cycle_ok = now

        except Exception as e:
            logger.error(f"Refresh loop error: {e}")

        elapsed = time.monotonic() - cycle_clock
        ct.commit(elapsed)
        # Page on a genuine stall so a freeze reaches you instead of going silent.
        try:
            page = cycle_watchdog.alert_message(now, now - SERVER_STARTED_AT)
            if page:
                notifier.send(page)
            feed_page = cycle_watchdog.feed_alert_message(now)
            if feed_page:
                cycle_watchdog.send_dependency_free_telegram(feed_page)
        except Exception:
            logger.exception("watchdog pager failed")
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            executor.shutdown(wait=False)
            detail_executor.shutdown(wait=False)
            return
        time.sleep(max(0.0, REFRESH_INTERVAL - elapsed))


@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp








_COMPACT_FIELDS = (


    "q15_v9_4_version", "q15_v9_4_context_status",


    "q15_v9_4_context_relation", "q15_v9_4_context_score",


    "q15_v9_4_decision", "q15_v9_4_entry_allowed",


    "q15_v9_4_previous_15m", "q15_v9_4_current_15m",

    "asset", "ticker", "market_state", "seconds_remaining", "close_time",
    "entry_status", "entry_side", "estimated_edge", "estimated_fair_probability",
    "fee_adjusted_break_even", "spread", "pattern_detected", "pattern_direction",
    "opportunity_score", "underlying_current", "target_distance_pct",
    "q15_prediction_agreement", "q15_final_result", "q15_focus_rank", "q15_in_top3",
    "q15_new_entry_allowed", "q15_trade_plan", "q15_management_plan",
    "calibrated_edge_status", "calibrated_edge_side", "calibrated_edge_new_entry_allowed",
    "raw_side_probability", "conservative_side_probability", "market_implied_side_probability",
    "point_edge_after_costs_cents", "conservative_edge_after_costs_cents",
    "calibrated_max_entry_price", "calibrated_ideal_entry_zone",
    "calibrated_management",
    "q15_v7_version", "q15_v7_shadow_decision", "q15_v7_shadow_ready",
    "q15_v7_shadow_blockers", "q15_v7_decision_key",
    "q15_v7_transition_valid", "q15_v7_sanity_issues",
    "end_prediction_result", "end_prediction_yes_probability",
    "end_prediction_confidence", "predicted_final_underlying",
    "data_age_seconds", "spot_age_seconds", "orderbook_age_seconds",
    "v5_data_valid", "paper_suggested_contracts", "paper_worst_case_loss",
    "q15_v9_4_unified_version",
    "q15_v9_4_unified_probability_yes",
    "q15_v9_4_unified_probability_no",
    "q15_v9_4_unified_selected_side",
    "q15_v9_4_unified_trade_decision",
    "q15_v9_4_unified_entry_allowed",
    "q15_v9_4_unified_data_quality",
    "q15_v9_4_unified_net_edge_cents",
    "q15_v9_4_unified_rank",
    "q15_v9_5_version",
    "q15_v9_5_selected_side",
    "q15_v9_5_yes_probability",
    "q15_v9_5_no_probability",
    "q15_v9_5_conservative_probability",
    "q15_v9_5_data_quality",
    "q15_v9_5_evidence_quality",
    "q15_v9_5_trade_quality",
    "q15_v9_5_trade_decision",
    "q15_v9_5_net_edge_cents",
    "q15_v9_5_ideal_entry_cents",
    "q15_v9_5_regime",
    "q15_v9_5_entry_allowed",
    "q15_v9_5_manipulation_suspected",
    "q15_v9_5_manipulation_reason",
    "q15_v9_5_manipulation_lean",
    "q15_v9_5_flip_risk_score",
    "q15_v9_5_flip_risk_confidence",
    "q15_v9_5_flip_risk_primary_reason",
    "q15_v9_5_flip_risk_direction",
    "q15_v9_5_flip_risk_evidence_count",
    "q15_v9_5_flip_threshold",
    "q15_v9_5_flip_threshold_source",
    "q15_v9_5_flip_threshold_status",
    "q15_v9_5_flip_samples",
    "q15_v9_5_flip_probability",
    "q15_v9_5_flip_state",
    "q15_v9_5_flip_dashboard",
    "q15_v9_5_rank",
    "q15_v9_5_top_pick",
)

























































_refresh_started = False
_refresh_lock = threading.Lock()


def _start_refresh():
    global _refresh_started
    with _refresh_lock:
        if not _refresh_started:
            try:
                from spot_depth import start_spot_depth
                start_spot_depth()
            except Exception as exc:
                logger.warning("Spot depth collector did not start: %s", exc)
            try:
                from spot_l3 import start_spot_l3
                start_spot_l3()
            except Exception as exc:
                logger.warning("Coinbase L3 collector did not start: %s", exc)
            try:
                from coinbase_adv_l2 import start_coinbase_adv_l2
                start_coinbase_adv_l2()
            except Exception as exc:
                logger.warning("Coinbase Advanced L2 collector did not start: %s", exc)
            try:
                from kraken_l3 import start_kraken_l3
                start_kraken_l3()
            except Exception as exc:
                logger.warning("Kraken L3 collector did not start: %s", exc)
            try:
                from settlement_index import start_settlement_index
                start_settlement_index()
            except Exception as exc:
                logger.warning("Settlement index collector did not start: %s", exc)
            try:
                from ladder_probe import start_ladder_probe
                start_ladder_probe()
            except Exception as exc:
                logger.warning("Ladder probe did not start: %s", exc)
            try:
                from market_activity import start_market_activity
                start_market_activity()
            except Exception as exc:
                logger.warning("Market activity recorder did not start: %s", exc)
            try:
                from path_recorder import start_path_recorder
                start_path_recorder()
            except Exception as exc:
                logger.warning("Path recorder did not start: %s", exc)
            try:
                from liq_feed import start_liq_feed
                start_liq_feed()
            except Exception as exc:
                logger.warning("Liquidation feed did not start: %s", exc)
            threading.Thread(target=refresh_loop, daemon=True).start()
            _refresh_started = True
            logger.info("Refresh loop started")


# ---- Route registration (Stage 2 refactor) ------------------------------
# All HTTP routes live in routes/. Registered here, after every singleton
# above exists; bodies resolve host globals lazily so this ordering is the
# only constraint. sys.modules[__name__] (not "import app") keeps this safe
# under both `python3 app.py` (__main__) and `import app` (tests).
import routes as _routes  # noqa: E402  (deliberate late import; see above)

_routes.register_all(app, sys.modules[__name__])

# Autostart on import keeps deployment behavior unchanged. Set
# Q15_AUTOSTART_REFRESH=0 to import the app (routes, globals) without spawning
# the live cycle — used by tests and offline diagnostics.
if os.environ.get("Q15_AUTOSTART_REFRESH", "1") != "0":
    _start_refresh()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Kalshi 15-Min Monitor on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
