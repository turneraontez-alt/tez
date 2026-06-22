import os
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
_bi = build_version.build_info()
logger.info("BUILD %s (%s) shipped=%s — %s · tests=%s",
            _bi.get("commit"), _bi.get("branch"), _bi.get("committed_at"),
            _bi.get("summary"), _bi.get("tests"))


@app.route("/version")
def version_text_ep():
    # Human-readable in a browser: the build the RUNNING app is on.
    return app.response_class(build_version.version_text(), mimetype="text/plain")


@app.route("/api/version")
@app.route("/data/version")
def version_json_ep():
    return jsonify(build_version.version_payload())

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
    if isinstance(ob_raw, dict):
        for key in ("updated_at", "event_ts", "ts", "timestamp"):
            try:
                value = ob_raw.get(key)
                if value is not None:
                    book_event_ts = float(value)
                    break
            except (TypeError, ValueError):
                pass

    return {
        "asset": asset,
        "ticker": ticker,
        "ob_raw": ob_raw,
        "trades": trades,
        "spot": spot,
        "source": "ws" if market_data.is_connected() else "rest",
        "fetch_started_at": started,
        "fetch_completed_at": completed,
        "fetch_latency_seconds": round(completed - started, 4),
        "latest_trade_event_ts": latest_trade_event_ts,
        "book_event_ts": book_event_ts,
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
    cycles = 0
    while True:
        cycle_clock = time.monotonic()
        cycle_start = time.time()
        now = cycle_start
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
                    cycling.setdefault(asset, {"since": now, "last_try": 0})
                    current_markets.pop(asset, None)
                    logger.info(f"{asset} expired — cycling")

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
                try:
                    snap = eng.build_snapshot(
                        a, market, ob_parsed, ob_delta, r["spot"],
                        broader, r.get("source") == "ws", now,
                    )
                    snap = attach_orderbook_levels(snap, ob_parsed, market)
                    snap = apply_snapshot_freshness(
                        snap, r, now, market_data.health(), config
                    )
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/api/snapshot")
@app.route("/data/snapshot")
def snapshot():
    with state_lock:
        data = list(state.values())
    data.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)
    return jsonify(data)


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


@app.route("/api/compact")
@app.route("/data/compact")
def compact():
    with state_lock:
        data = list(state.values())
    out = []
    for d in data:
        rec = {k: d.get(k) for k in _COMPACT_FIELDS}
        rec["confirmation_count"] = len(d.get("confirmation_reasons") or [])
        out.append(rec)
    out.sort(key=lambda x: (x.get("estimated_edge") or -999), reverse=True)
    return jsonify(out)


@app.route("/api/summary")
@app.route("/data/summary")
def summary():
    sigs = signal_engine.get_active_signals()
    lines = []
    for s in sigs:
        edge = s.get("edge")
        edge_s = f"{edge:+.1f}pp" if isinstance(edge, (int, float)) else "n/a"
        alt = " (alt)" if s.get("is_alternative") else ""
        lines.append(
            f"{s['state']}: {s['asset']} {s.get('side') or ''} "
            f"edge {edge_s} conf {s.get('confirmation_count')}{alt}"
        )
    text = "\n".join(lines) if lines else "No active signals \u2014 NO TRADE across all markets."
    return jsonify({"summary": text, "active_count": len(sigs),
                    "generated_at": _iso_now()})


@app.route("/api/signals")
@app.route("/data/signals")
def signals_ep():
    return jsonify(signal_engine.get_active_signals())


@app.route("/api/alerts")
@app.route("/data/alerts")
def alerts_ep():
    limit = 100
    return jsonify(store.recent_alerts(limit))


@app.route("/api/performance")
@app.route("/data/performance")
def performance_ep():
    return jsonify(perf.stats())


@app.route("/api/learning")
@app.route("/data/learning")
def learning_ep():
    payload = learner.summary()
    if not isinstance(payload, dict):
        payload = {"legacy": payload}
    payload["two_window"] = focus_manager.learning_summary()
    return jsonify(payload)


@app.route("/api/end-predictions")
@app.route("/data/end-predictions")
def end_predictions_ep():
    return jsonify(learner.end_predictions())




















@app.route("/api/q15-v9-4/unified/predictions")
def q15_v94_unified_predictions_ep():
    return jsonify(checkpoint_v95.unified_predictions())

@app.route("/api/q15-v9-4/unified/learning")
def q15_v94_unified_learning_ep():
    return jsonify(checkpoint_v95.unified_learning_status())

@app.route("/api/q15-v9-4/unified/decision-stats")
def q15_v94_unified_decision_stats_ep():
    return jsonify(checkpoint_v95.decision_stats())



@app.route("/api/q15-v9-5/diagnostics")
def q15_v95_diagnostics_ep():
    return jsonify(checkpoint_v95.diagnostics())

@app.route("/api/q15-v9-5/predictions")
def q15_v95_predictions_ep():
    return jsonify(checkpoint_v95.predictions())

@app.route("/api/q15-v9-5/market-data")
def q15_v95_market_data_ep():
    return jsonify(checkpoint_v95.market_data_status())

@app.route("/api/q15-v9-5/calibration")
def q15_v95_calibration_ep():
    return jsonify(checkpoint_v95.calibration_status())

@app.route("/api/q15-v9-5/learning")
def q15_v95_learning_ep():
    return jsonify(checkpoint_v95.learning_status())

@app.route("/api/q15-v9-5/decision-stats")
def q15_v95_decision_stats_ep():
    return jsonify(checkpoint_v95.decision_stats())

@app.route("/api/q15-v9-5/scoreboard")
@app.route("/data/q15-v9-5/scoreboard")
def q15_v95_scoreboard_ep():
    return jsonify(checkpoint_v95.scoreboard())

@app.route("/api/q15-v9-5/accuracy")
@app.route("/data/q15-v9-5/accuracy")
def q15_v95_accuracy_ep():
    return jsonify(checkpoint_v95.accuracy_report())

@app.route("/api/q15-v9-5/shadow-signals")
@app.route("/data/q15-v9-5/shadow-signals")
def q15_v95_shadow_signals_ep():
    return jsonify(checkpoint_v95.shadow_signal_experiment())

@app.route("/api/market-cache")
@app.route("/data/market-cache")
def market_cache_ep():
    return jsonify(market_cache.stats())

@app.route("/api/q15-v9-4/diagnostics")
def q15_v94_diagnostics_ep():
    return jsonify(checkpoint_v95.diagnostics())

@app.route("/api/q15-v9-4/context")
def q15_v94_context_ep():
    return jsonify(checkpoint_v95.context_status())

@app.route("/api/q15-v9-4/decision-stats")
def q15_v94_decision_stats_ep():
    return jsonify(checkpoint_v95.decision_stats())

@app.route("/api/q15-v9-3/diagnostics")
def q15_v93_diagnostics_ep():
    return jsonify(checkpoint_v95.diagnostics())

@app.route("/api/q15-v9-3/wick-analysis")
def q15_v93_wick_analysis_ep():
    return jsonify(checkpoint_v95.wick_status())

@app.route("/api/q15-v9-3/decision-stats")
def q15_v93_decision_stats_ep():
    return jsonify(checkpoint_v95.decision_stats())

@app.route("/api/q15-v9-2/diagnostics")
def q15_v92_diagnostics_ep():
    return jsonify(checkpoint_v95.diagnostics())

@app.route("/api/q15-v9-2/consensus")
def q15_v92_consensus_ep():
    return jsonify(checkpoint_v95.consensus_status())

@app.route("/api/q15-v9-2/decision-stats")
def q15_v92_decision_stats_ep():
    return jsonify(checkpoint_v95.decision_stats())

@app.route("/api/q15-v9-1/diagnostics")
def q15_v91_diagnostics_ep():
    return jsonify(checkpoint_v95.diagnostics())

@app.route("/api/q15-v9-1/rolling")
def q15_v91_rolling_ep():
    return jsonify(checkpoint_v95.rolling_status())

@app.route("/api/q15-v9-1/predictions")
def q15_v91_predictions_ep():
    return jsonify(checkpoint_v95.predictions_status())

@app.route("/api/q15-v9/backtest")
def q15_v9_backtest_ep():
    return jsonify(oos_v9.backtest_report())

@app.route("/api/q15-v9/out-of-sample")
def q15_v9_oos_ep():
    return jsonify(oos_v9.out_of_sample_report())

@app.route("/api/q15-v9/model-comparison")
def q15_v9_model_comparison_ep():
    return jsonify(oos_v9.model_comparison())

@app.route("/api/q15-v9/telegram-outbox")
def q15_v9_telegram_outbox_ep():
    return jsonify({"version": "q15-v9-telegram-outbox", "rows": telegram_outbox.rows(100), "read_only": True})

@app.route("/api/q15-v9/telegram-health")
def q15_v9_telegram_health_ep():
    return jsonify(telegram_outbox.health())

@app.route("/api/q15-v9/dead-letters")
def q15_v9_dead_letters_ep():
    return jsonify({"version": "q15-v9-telegram-outbox", "rows": telegram_outbox.dead_letters(100), "read_only": True})

@app.route("/api/q15-v9/diagnostics")
def q15_v9_diagnostics_ep():
    oos = oos_v9.out_of_sample_report()
    return jsonify({
        "version": "q15-v9-edge-proof-alert-reliability",
        "probability_pipeline": "market baseline used once + independent residual",
        "economics_version": "q15-v9-canonical-economics",
        "duplicate_market_anchor_check": "patched",
        "side_preservation": "selected side is used for probability, quote, and economics",
        "checkpoint_sequence": "15M independent; 10M compares with saved 15M",
        "telegram_outbox": telegram_outbox.health(),
        "out_of_sample_metric_availability": bool(oos.get("available")),
        "out_of_sample_reason": oos.get("reason"),
        "read_only": True,
    })

@app.route("/api/q15-v7/diagnostics")
@app.route("/data/q15-v7/diagnostics")
def q15_v7_diagnostics_ep():
    return jsonify(professional_v7.summary())


@app.route("/api/q15-v7/near-misses")
def q15_v7_near_misses_ep():
    return jsonify({"version": "q15-v7-professional-liveness", "near_misses": professional_v7.near_misses(50), "read_only": True})


@app.route("/api/q15-v7/liveness")
def q15_v7_liveness_ep():
    return jsonify(professional_v7.liveness_diagnosis())


@app.route("/api/q15-v7/gate-ablation")
def q15_v7_gate_ablation_ep():
    return jsonify({"version": "q15-v7-professional-liveness", "gate_ablation": professional_v7.gate_ablation(), "read_only": True})


@app.route("/api/q15-v7/threshold-sensitivity")
def q15_v7_threshold_sensitivity_ep():
    return jsonify(professional_v7.threshold_sensitivity())


@app.route("/api/q15-v7/hourly-report")
def q15_v7_hourly_report_ep():
    return jsonify({"version": "q15-v7-professional-liveness", "message": professional_v7.hourly_report(), "read_only": True})

@app.route("/api/calibrated-edge")
@app.route("/data/calibrated-edge")
def calibrated_edge_ep():
    return jsonify(calibrated_edge.summary())

@app.route("/api/focus")
@app.route("/data/focus")
def focus_ep():
    return jsonify(focus_manager.focus_status())


@app.route("/api/two-predictions")
@app.route("/data/two-predictions")
def two_predictions_ep():
    return jsonify(focus_manager.predictions_status())

@app.route("/api/final10m-review")
@app.route("/data/final10m-review")
def final10m_review_ep():
    return jsonify(focus_manager.self_review_status())

@app.route("/api/scalps")
@app.route("/data/scalps")
def scalps_ep():
    return jsonify({"record": scalp_engine.record(),
                    "open": scalp_engine.open_positions()})


@app.route("/api/report-preview")
@app.route("/data/report-preview")
def report_preview_ep():
    return jsonify({"report": reporter.build_report()})


@app.route("/api/health")
@app.route("/data/health")
def health():
    now = time.time()
    with state_lock:
        snaps = list(state.values())
        live = [s for s in snaps if s.get("market_state") == "live"]
        ages = []
        for a in ASSETS:
            s = next((x for x in live if x.get("asset") == a), None)
            ts = engine_update_ts.get(a)
            if s is not None and ts:
                ages.append(now - ts)
    data_age = round(max(ages), 2) if ages else None

    closes = sorted([s.get("close_time") for s in live if s.get("close_time")])
    secs = next((s.get("seconds_remaining") for s in live if s.get("close_time") == closes[0]), None) if closes else None
    current_window = {"close_time": closes[0], "seconds_remaining": secs} if closes else None

    try:
        wsh = market_data.health() or {}
    except Exception:
        wsh = {}

    try:
        from spot_ws import spot_ws_health
        spot_ws_status = spot_ws_health()
    except Exception:
        spot_ws_status = {"enabled": False}

    deployment_type = "reserved-vm" if os.environ.get("REPLIT_DEPLOYMENT") else "development"

    # Surface learning-ledger health at the top level so silent learning-layer
    # degradation is visible without digging into q15_v9_5.ledger. The owner trades
    # off these alerts, so a calibration that has silently fallen back to identity
    # (calibration_unconverged_fallbacks) or a shadow challenger that has stopped
    # learning (shadow_errors / last_shadow_error) must surface here, not just in
    # logs. Never let a ledger hiccup break the health route itself.
    try:
        ls = checkpoint_v95.ledger.status()
        ledger_health = {
            "available": bool(ls.get("available")),
            "path": ls.get("path"),
            "error": ls.get("error"),
            "unique_predictions": ls.get("unique_predictions"),
            "unique_resolved": ls.get("unique_resolved"),
            "dropped_feature_rows": ls.get("dropped_feature_rows"),
            "calibration_unconverged_fallbacks": ls.get("calibration_unconverged_fallbacks"),
            "shadow_errors": ls.get("shadow_errors"),
            "last_shadow_error": ls.get("last_shadow_error"),
        }
    except Exception as e:
        ledger_health = {"available": False, "error": f"{type(e).__name__}: {e}"}

    return jsonify({
        "status": "ok",
        "ledger": ledger_health,
        "q15_v9_5": checkpoint_v95.health(),
        "q15_v9_1": checkpoint_v95.health(),
        "q15_v9_2": checkpoint_v95.health(),
        "q15_v9_3": checkpoint_v95.health(),
        "q15_v9_4": checkpoint_v95.health(),
        "two_prediction_focus": focus_manager.health(),
        "calibrated_edge": calibrated_edge.health(),
        "q15_v7": professional_v7.health(),
        "q15_v9": {
            "version": "q15-v9-edge-proof-alert-reliability",
            "enabled": True, "read_only": True,
            "canonical_economics": True,
            "persistent_telegram_outbox": True,
            "out_of_sample_framework": True,
            "live_parameter_updates": False,
            "telegram": telegram_outbox.health(),
            "resolved_outcome_metrics_available": bool(oos_v9.out_of_sample_report().get("available")),
        },
        "server_started_at": SERVER_STARTED_AT_ISO,
        "uptime_seconds": round(now - SERVER_STARTED_AT),
        "websocket_connected": bool(wsh.get("connected")),
        "websocket_last_message_at": wsh.get("last_message_at"),
        "websocket_book_ages": wsh.get("book_ages"),
        "spot_ws": spot_ws_status,
        "cycle_watchdog": cycle_watchdog.health(),
        "current_market_window": current_window,
        "assets_subscribed": [s.get("asset") for s in live],
        "assets_tracked": len(snaps),
        "telegram_status": notifier.status(),
        "model_accuracy": checkpoint_v95.accuracy_summary(),
        "data_age_seconds": data_age,
        "alerts_generated": store.count_alerts(),
        "deployment_type": deployment_type,
        "mode": "ws-primary" if wsh.get("connected") else "rest-polling",
        "persistence": "postgres" if store.enabled else "disabled",
        "last_successful_cycle_age_s": round(now - _last_cycle_ok, 2) if _last_cycle_ok else None,
        "q15_settings": asdict(upgrade.settings),
        "learning_v4": learner.health_summary(),
        "config": config.as_dict(),
    })


_refresh_started = False
_refresh_lock = threading.Lock()


def _start_refresh():
    global _refresh_started
    with _refresh_lock:
        if not _refresh_started:
            threading.Thread(target=refresh_loop, daemon=True).start()
            _refresh_started = True
            logger.info("Refresh loop started")


# Autostart on import keeps deployment behavior unchanged. Set
# Q15_AUTOSTART_REFRESH=0 to import the app (routes, globals) without spawning
# the live cycle — used by tests and offline diagnostics.
if os.environ.get("Q15_AUTOSTART_REFRESH", "1") != "0":
    _start_refresh()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Kalshi 15-Min Monitor on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
