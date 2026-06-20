import os
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify

from q15_upgrade.kalshi_rest import KalshiClient
from spot_client import get_spot
from q15_upgrade.orderbook import parse_orderbook, OrderbookTracker
from analysis import AssetEngine
from alert_config import AlertConfig
from db import SignalStore
from notifier import TelegramNotifier
from q15_upgrade.signals import SignalEngine
from performance import PerformanceTracker
from q15_upgrade.learning import LearningEngine
from scalp import ScalpEngine
from reporting import HourlyReporter
from q15_upgrade.runtime import Q15Runtime, attach_orderbook_levels
from q15_upgrade.store_patch import patch_store
from q15_upgrade.hybrid_data import HybridMarketData
from q15_upgrade.ws_client import get_feed as get_ws_feed
from q15_upgrade.window_focus import TwoWindowFocusManager  # Q15_TWO_WINDOW_APP_INTEGRATION
from q15_upgrade.calibrated_edge import CalibratedEdgeEngine  # Q15_V6_CALIBRATED_EDGE

from q15_upgrade.v5_hardening import (
    apply_snapshot_freshness,
    enforce_fail_closed,
    risk_preview,
)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

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

client = KalshiClient()
engines = {a: AssetEngine(a) for a in ASSETS}
# Q15_V5_APP_HARDENING: parser and tracker now use the same schema.
for _engine in engines.values():
    _engine.ob_tracker = OrderbookTracker()
state = {}
state_lock = threading.Lock()
_last_detail = {a: 0 for a in ASSETS}

# Entry-alert subsystem (read-only; never places orders).
config = AlertConfig()
ws_feed = get_ws_feed() if config.ws_enabled else None
market_data = HybridMarketData(client, ws_feed)
store = patch_store(SignalStore())
notifier = TelegramNotifier()
learner = LearningEngine(store, config, notifier=notifier)  # Q15_V4_LEARNING_INTEGRATION
upgrade = Q15Runtime(config, learner)
logger.info("Q15 settings active: %s", asdict(upgrade.settings))
signal_engine = SignalEngine(config, store, notifier, learner)
perf = PerformanceTracker(store, client)
scalp_engine = ScalpEngine(store, notifier, config)
scalp_engine.learning_v2 = learner
reporter = HourlyReporter(store, notifier, config, perf, learner, scalp_engine)

focus_manager = TwoWindowFocusManager(store, notifier, config, client)

calibrated_edge = CalibratedEdgeEngine(store, notifier, config, learner)
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


def fetch_asset_raw(asset, market, now, last_trade_ts, want_detail):
    """Network worker that preserves source timestamps for freshness gates."""
    started = time.time()
    ticker = market["ticker"]
    detail = client.get_market(ticker) if want_detail else None
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
        "detail": detail,
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


def refresh_loop():
    last_discovery = 0
    current_markets = {}
    cycling = {}
    inflight = {}  # asset -> Future (at most one outstanding fetch per asset)
    executor = ThreadPoolExecutor(max_workers=8)
    while True:
        cycle_clock = time.monotonic()
        cycle_start = time.time()
        now = cycle_start
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
            market_data.subscribe([m.get("ticker") for m in active.values() if m.get("ticker")])

            # -- concurrent fetch (network only) --
            # Per-asset in-flight tracking: at most one outstanding request per
            # asset, so a persistently slow upstream cannot pile work onto the
            # executor and starve other assets' fetches.
            results = {}
            # 1) harvest any previous-cycle requests that have since finished
            for a in list(inflight.keys()):
                f = inflight[a]
                if f.done():
                    try:
                        results[a] = f.result()
                    except Exception as e:
                        logger.warning(f"fetch {a}: {e}")
                    del inflight[a]
            # 2) submit a fresh request only for active assets not already in flight
            futs = {}
            for asset, market in active.items():
                if asset in inflight:
                    continue
                want_detail = (now - _last_detail[asset]) >= DETAIL_INTERVAL
                if want_detail:
                    _last_detail[asset] = now
                f = executor.submit(
                    fetch_asset_raw, asset, market, now,
                    engines[asset].last_trade_ts, want_detail,
                )
                inflight[asset] = f
                futs[f] = asset
            # 3) wait up to the per-cycle deadline for THIS cycle's requests;
            #    slow ones stay in flight and are harvested in a later cycle so
            #    one upstream stall can't freeze the whole dashboard.
            try:
                for fut in as_completed(list(futs.keys()), timeout=FETCH_DEADLINE):
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
                logger.warning(
                    f"fetch deadline {FETCH_DEADLINE}s exceeded; deferring {slow}"
                )

            # -- ingest sequentially into engines --
            prelim = {}
            for a, r in results.items():
                # Drop late in-flight results whose market has rolled over or
                # is no longer active (avoids resetting an engine to a stale
                # ticker and keeps the snapshot loop's active[a] lookups safe).
                if a not in active or active[a].get("ticker") != r.get("ticker"):
                    continue
                eng = engines[a]
                eng.ensure_market(r["ticker"])
                eng.ingest_trades(r["trades"], now)
                eng.ingest_spot(r["spot"], now)
                ob_parsed = parse_orderbook(r["ob_raw"])
                yb, ya = ob_parsed["yes_bid"], ob_parsed["yes_ask"]
                ob_parsed["spread"] = (ya - yb) if (yb is not None and ya is not None) else None
                ob_delta = eng.ob_tracker.update(ob_parsed)
                prelim[a] = (r, ob_parsed, ob_delta)

            # -- broader market direction (BTC/ETH underlying 60s) --
            broader = {}
            for k in ("BTC", "ETH"):
                if k in engines:
                    ch = engines[k].underlying_change_pct(now, 60)
                    broader[k] = (1 if ch > 0.02 else -1 if ch < -0.02 else 0) if ch is not None else 0

            # -- build snapshots --
            for a, (r, ob_parsed, ob_delta) in prelim.items():
                eng = engines[a]
                market = {**active[a], **(r["detail"] or {})}
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
            snaps = focus_manager.pre_enrich(snaps, now)
            snaps = upgrade.enrich_all(snaps, now, ws_health)
            snaps = learner.enrich_and_observe(snaps, now, ws_health)
            snaps = {
                asset: risk_preview(enforce_fail_closed(snap))
                for asset, snap in snaps.items()
            }
            snaps = calibrated_edge.preview_all(snaps, now, ws_health)
            snaps = focus_manager.update(snaps, now, ws_health)
            snaps = calibrated_edge.enrich_all(snaps, now, ws_health)
            with state_lock:
                state.update(snaps)
            deep_snaps = focus_manager.deep_evaluation_snapshots(snaps, signal_engine, scalp_engine)
            _safe("signals", signal_engine.evaluate_all, deep_snaps, now, ws_health)
            _safe("scalp", scalp_engine.evaluate, deep_snaps, now)
            _safe("focus_settlement", focus_manager.reconcile_settlements, now)
            _safe("report", reporter.maybe_send, now)
            if now - _last_learn >= 10:           # heavy DB work: every 10s, not 1s
                _safe("perf", perf.reconcile, now)
                _safe("learning_reconcile", learner.reconcile, now, client)
                _safe("learner", learner.recompute, now)
                _last_learn = now
            _last_cycle_ok = now

        except Exception as e:
            logger.error(f"Refresh loop error: {e}")

        elapsed = time.monotonic() - cycle_clock
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
    "end_prediction_result", "end_prediction_yes_probability",
    "end_prediction_confidence", "predicted_final_underlying",
    "data_age_seconds", "spot_age_seconds", "orderbook_age_seconds",
    "v5_data_valid", "paper_suggested_contracts", "paper_worst_case_loss",
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
            if s is not None and engines[a].last_update_ts:
                ages.append(now - engines[a].last_update_ts)
    data_age = round(max(ages), 2) if ages else None

    closes = sorted([s.get("close_time") for s in live if s.get("close_time")])
    secs = next((s.get("seconds_remaining") for s in live if s.get("close_time") == closes[0]), None) if closes else None
    current_window = {"close_time": closes[0], "seconds_remaining": secs} if closes else None

    try:
        wsh = market_data.health() or {}
    except Exception:
        wsh = {}

    deployment_type = "reserved-vm" if os.environ.get("REPLIT_DEPLOYMENT") else "development"
    return jsonify({
        "status": "ok",
        "two_prediction_focus": focus_manager.health(),
        "calibrated_edge": calibrated_edge.health(),
        "server_started_at": SERVER_STARTED_AT_ISO,
        "uptime_seconds": round(now - SERVER_STARTED_AT),
        "websocket_connected": bool(wsh.get("connected")),
        "websocket_last_message_at": wsh.get("last_message_at"),
        "current_market_window": current_window,
        "assets_subscribed": [s.get("asset") for s in live],
        "assets_tracked": len(snaps),
        "telegram_status": notifier.status(),
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


_start_refresh()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Kalshi 15-Min Monitor on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
