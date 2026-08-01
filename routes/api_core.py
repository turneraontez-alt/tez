"""Flask routes extracted from app.py (Stage 2 refactor, behavior-preserving).

Each module exposes register(flask_app, host): ``host`` is the app module object
(``sys.modules[__name__]`` at the call site — NOT ``import app``, which would
re-execute app.py's wiring when the process runs as ``python3 app.py`` /
``__main__``). Route bodies reference host globals lazily via ``_app.<name>`` so
values rebound after startup (e.g. _last_cycle_ok) stay live. Endpoints keep
their original function names — the frozen route-table test pins this.
"""

import threading

_app = None
_health_cache = None
_health_cache_at = None
_health_cache_lock = threading.RLock()


def _github_relay_status() -> dict:
    """Read the relay's durable status file so a broken deploy path is VISIBLE.

    The relay reported a failed push only by printing to its own stdout log, so
    an expired GH_PUSH_TOKEN looked identical to healthy operation from here —
    commits silently stopped reaching GitHub. ``ok`` is False once a push has
    failed consecutively, which is the signal worth alerting on. Missing file =
    the relay has not run (or is an older build), reported as ``unknown`` rather
    than a failure. Never raises: health must not break on a bad status file.
    """
    import json
    import os
    import time

    path = os.environ.get("GITHUB_RELAY_STATUS_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "work", "local-run", "relay_status.json")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {"ok": None, "state": "unknown", "detail": "no relay status file"}
    if not isinstance(raw, dict):
        return {"ok": None, "state": "unknown", "detail": "malformed relay status"}
    fails = raw.get("consecutive_push_failures") or 0
    updated = raw.get("updated_at")
    return {
        "ok": fails == 0,
        "state": "failing" if fails else "ok",
        "consecutive_push_failures": fails,
        "last_push_ok_at": raw.get("last_push_ok_at"),
        "last_push_error": raw.get("last_push_error"),
        "last_push_error_at": raw.get("last_push_error_at"),
        "status_age_seconds": (round(time.time() - float(updated), 1)
                               if isinstance(updated, (int, float)) else None),
        "local": raw.get("local"),
        "remote": raw.get("remote"),
    }


def _exact_capture_guard_state(now: float) -> dict:
    """Protect the 60-second independent-path history from health contention."""
    epoch = int(float(now))
    phase = (epoch % 900 + 900) % 900
    capture_phase = 120
    seconds_until = (capture_phase - phase + 900) % 900
    seconds_since = (phase - capture_phase + 900) % 900
    protected_before_seconds = 75
    protected_after_seconds = 5
    protected = bool(
        seconds_until <= protected_before_seconds
        or seconds_since <= protected_after_seconds
    )
    return {
        "protected": protected,
        "capture_phase_seconds": capture_phase,
        "phase_seconds": phase,
        "seconds_until_exact_capture": seconds_until,
        "seconds_since_exact_capture": seconds_since,
        "protected_before_seconds": protected_before_seconds,
        "protected_after_seconds": protected_after_seconds,
    }


def register(flask_app, host):
    global _app
    _app = host

    @flask_app.route("/version")
    def version_text_ep():
        # Human-readable in a browser: the build the RUNNING app is on.
        return _app.app.response_class(_app.build_version.version_text(), mimetype="text/plain")

    @flask_app.route("/api/version")
    @flask_app.route("/data/version")
    def version_json_ep():
        return _app.jsonify(_app.build_version.version_payload())

    @flask_app.route("/")
    def index():
        return _app.render_template("index.html")

    @flask_app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @flask_app.route("/api/snapshot")
    @flask_app.route("/data/snapshot")
    def snapshot():
        with _app.state_lock:
            data = list(_app.state.values())
        data.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)
        return _app.jsonify(data)

    @flask_app.route("/api/compact")
    @flask_app.route("/data/compact")
    def compact():
        with _app.state_lock:
            data = list(_app.state.values())
        out = []
        for d in data:
            rec = {k: d.get(k) for k in _app._COMPACT_FIELDS}
            rec["confirmation_count"] = len(d.get("confirmation_reasons") or [])
            out.append(rec)
        out.sort(key=lambda x: (x.get("estimated_edge") or -999), reverse=True)
        return _app.jsonify(out)

    @flask_app.route("/api/summary")
    @flask_app.route("/data/summary")
    def summary():
        sigs = _app.signal_engine.get_active_signals()
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
        return _app.jsonify({"summary": text, "active_count": len(sigs),
                        "generated_at": _app._iso_now()})

    @flask_app.route("/api/signals")
    @flask_app.route("/data/signals")
    def signals_ep():
        return _app.jsonify(_app.signal_engine.get_active_signals())

    @flask_app.route("/api/alerts")
    @flask_app.route("/data/alerts")
    def alerts_ep():
        limit = 100
        return _app.jsonify(_app.store.recent_alerts(limit))

    @flask_app.route("/api/performance")
    @flask_app.route("/data/performance")
    def performance_ep():
        return _app.jsonify(_app.perf.stats())

    @flask_app.route("/api/learning")
    @flask_app.route("/data/learning")
    def learning_ep():
        payload = _app.learner.summary()
        if not isinstance(payload, dict):
            payload = {"legacy": payload}
        payload["two_window"] = _app.focus_manager.learning_summary()
        return _app.jsonify(payload)

    @flask_app.route("/api/end-predictions")
    @flask_app.route("/data/end-predictions")
    def end_predictions_ep():
        return _app.jsonify(_app.learner.end_predictions())

    @flask_app.route("/api/market-cache")
    @flask_app.route("/data/market-cache")
    def market_cache_ep():
        return _app.jsonify(_app.market_cache.stats())

    @flask_app.route("/api/calibrated-edge")
    @flask_app.route("/data/calibrated-edge")
    def calibrated_edge_ep():
        return _app.jsonify(_app.calibrated_edge.summary())

    @flask_app.route("/api/focus")
    @flask_app.route("/data/focus")
    def focus_ep():
        return _app.jsonify(_app.focus_manager.focus_status())

    @flask_app.route("/api/two-predictions")
    @flask_app.route("/data/two-predictions")
    def two_predictions_ep():
        return _app.jsonify(_app.focus_manager.predictions_status())

    @flask_app.route("/api/final10m-review")
    @flask_app.route("/data/final10m-review")
    def final10m_review_ep():
        return _app.jsonify(_app.focus_manager.self_review_status())

    @flask_app.route("/api/scalps")
    @flask_app.route("/data/scalps")
    def scalps_ep():
        return _app.jsonify({"record": _app.scalp_engine.record(),
                        "open": _app.scalp_engine.open_positions()})

    @flask_app.route("/api/report-preview")
    @flask_app.route("/data/report-preview")
    def report_preview_ep():
        return _app.jsonify({"report": _app.reporter.build_report()})

    @flask_app.route("/api/health")
    @flask_app.route("/data/health")
    def health():
        global _health_cache, _health_cache_at
        now = _app.time.time()
        capture_guard = _exact_capture_guard_state(now)
        # Building the complete health graph can monopolize the Python process
        # for several seconds.  During the exact path's required history, serve
        # the most recent immutable snapshot instead.  Tests do not start the
        # live refresh loop, so this operational guard cannot mask test state.
        if bool(getattr(_app, "_refresh_started", False)) and capture_guard[
            "protected"
        ]:
            with _health_cache_lock:
                cached = _health_cache
                cached_at = _health_cache_at
            cache_meta = {
                **capture_guard,
                "served_cached": cached is not None,
                "reason": "EXACT_INDEPENDENT_PATH_COLLECTION_GUARD",
                "cached_at": cached_at,
                "cache_age_seconds": (
                    None if cached_at is None else max(0.0, now - cached_at)
                ),
            }
            if cached is None:
                return _app.jsonify({
                    "status": "capture_guard_cache_warming",
                    "health_cache": cache_meta,
                })
            payload = dict(cached)
            payload["health_cache"] = cache_meta
            return _app.jsonify(payload)
        with _app.state_lock:
            snaps = list(_app.state.values())
            live = [s for s in snaps if s.get("market_state") == "live"]
            ages = []
            for a in _app.ASSETS:
                s = next((x for x in live if x.get("asset") == a), None)
                ts = _app.engine_update_ts.get(a)
                if s is not None and ts:
                    ages.append(now - ts)
        data_age = round(max(ages), 2) if ages else None

        closes = sorted([s.get("close_time") for s in live if s.get("close_time")])
        secs = next((s.get("seconds_remaining") for s in live if s.get("close_time") == closes[0]), None) if closes else None
        current_window = {"close_time": closes[0], "seconds_remaining": secs} if closes else None

        try:
            wsh = _app.market_data.health() or {}
        except Exception:
            wsh = {}

        try:
            from spot_ws import spot_ws_health
            spot_ws_status = spot_ws_health()
        except Exception:
            spot_ws_status = {"enabled": False}

        try:
            from spot_depth import spot_depth_health
            spot_depth_status = spot_depth_health()
        except Exception:
            spot_depth_status = {"enabled": False}

        try:
            from spot_l3 import spot_l3_health
            spot_l3_status = spot_l3_health()
        except Exception:
            spot_l3_status = {"enabled": False}

        try:
            from coinbase_adv_l2 import coinbase_adv_l2_health
            coinbase_adv_l2_status = coinbase_adv_l2_health()
        except Exception:
            coinbase_adv_l2_status = {"enabled": False}

        try:
            from kraken_l3 import kraken_l3_health
            kraken_l3_status = kraken_l3_health()
        except Exception:
            kraken_l3_status = {"enabled": False}

        try:
            from settlement_index import settlement_index_health
            settlement_index_status = settlement_index_health()
        except Exception:
            settlement_index_status = {"enabled": False}

        try:
            from q15_upgrade.rti_exact_13m import exact_rti_13m_health
            exact_rti_13m_status = exact_rti_13m_health()
        except Exception:
            exact_rti_13m_status = {"enabled": False}

        try:
            from q15_upgrade.v11_readiness_monitor import (
                v11_readiness_monitor_health,
            )
            v11_readiness_monitor_status = v11_readiness_monitor_health()
        except Exception:
            v11_readiness_monitor_status = {
                "enabled": False,
                "paper_only": True,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
            }

        try:
            from q15_upgrade.v13_readiness_monitor import (
                v13_readiness_monitor_health,
            )
            v13_readiness_monitor_status = v13_readiness_monitor_health()
        except Exception:
            v13_readiness_monitor_status = {
                "enabled": False,
                "paper_only": True,
                "administrative_notices_only": True,
                "notification_is_trade_signal": False,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
            }

        try:
            from q15_upgrade.v14_readiness_monitor import (
                v14_readiness_monitor_health,
            )
            v14_readiness_monitor_status = v14_readiness_monitor_health()
        except Exception:
            v14_readiness_monitor_status = {
                "enabled": False,
                "paper_only": True,
                "administrative_notices_only": True,
                "notification_is_trade_signal": False,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
            }

        try:
            from q15_upgrade.independent_path_readiness_monitor import (
                independent_path_readiness_monitor_health,
            )
            independent_path_readiness_monitor_status = (
                independent_path_readiness_monitor_health()
            )
        except Exception:
            independent_path_readiness_monitor_status = {
                "enabled": False,
                "paper_only": True,
                "administrative_notices_only": True,
                "notification_is_trade_signal": False,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
                "feature_selection_performed": False,
                "thresholds_selected_from_outcomes": False,
            }

        try:
            from q15_upgrade.strategy_bots.runtime import (
                rti_path_13m_challenger_health_cached,
            )
            rti_path_13m_challenger_status = (
                rti_path_13m_challenger_health_cached()
            )
        except Exception:
            rti_path_13m_challenger_status = {
                "available": False,
                "paper_only": True,
                "id": "impulse_strength_v1",
                "notification_eligible": True,
                "automatic_promotion": False,
                "historical_credit_allowed": False,
            }

        try:
            from ladder_probe import ladder_health
            ladder_probe_status = ladder_health()
        except Exception:
            ladder_probe_status = {"enabled": False}

        try:
            from market_activity import market_activity_health
            market_activity_status = market_activity_health()
        except Exception:
            market_activity_status = {"enabled": False}

        try:
            from path_recorder import path_recorder_health
            path_recorder_status = path_recorder_health()
        except Exception:
            path_recorder_status = {"enabled": False}

        try:
            from q15_upgrade.path_forecast.runtime import path_forecast_health
            path_forecast_status = path_forecast_health()
        except Exception:
            path_forecast_status = {"enabled": False}

        try:
            from liq_feed import liq_health
            liq_feed_status = liq_health()
        except Exception:
            liq_feed_status = {"enabled": False}

        try:
            from strangle_shadow import strangle_shadow_health
            strangle_shadow_status = strangle_shadow_health(now=now)
        except Exception:
            strangle_shadow_status = {"enabled": False}

        deployment_type = "reserved-vm" if _app.os.environ.get("REPLIT_DEPLOYMENT") else "development"

        # Surface learning-ledger health at the top level so silent learning-layer
        # degradation is visible without digging into q15_v9_5.ledger. The owner trades
        # off these alerts, so a calibration that has silently fallen back to identity
        # (calibration_unconverged_fallbacks) or a shadow challenger that has stopped
        # learning (shadow_errors / last_shadow_error) must surface here, not just in
        # logs. Never let a ledger hiccup break the health route itself.
        ledger_status = None
        try:
            ledger_status = _app.checkpoint_v95.ledger.status()
            ledger_health = {
                "available": bool(ledger_status.get("available")),
                "path": ledger_status.get("path"),
                "error": ledger_status.get("error"),
                "unique_predictions": ledger_status.get("unique_predictions"),
                "unique_resolved": ledger_status.get("unique_resolved"),
                "dropped_feature_rows": ledger_status.get("dropped_feature_rows"),
                "calibration_unconverged_fallbacks": ledger_status.get("calibration_unconverged_fallbacks"),
                "shadow_errors": ledger_status.get("shadow_errors"),
                "last_shadow_error": ledger_status.get("last_shadow_error"),
            }
        except Exception as e:
            ledger_health = {"available": False, "error": f"{type(e).__name__}: {e}"}
            ledger_status = ledger_health

        try:
            grading_health = _app.checkpoint_v95.ledger.reconcile_backlog_status(now=now)
        except Exception as e:
            grading_health = {"available": False, "error": f"{type(e).__name__}: {e}"}

        try:
            q15_v95_health = _app.checkpoint_v95.health_compact(
                ledger_status=ledger_status,
                grading_status=grading_health,
                public_market_data=wsh,
            )
        except Exception as e:
            q15_v95_health = {"available": False, "error": f"{type(e).__name__}: {e}"}
        payload = {
            "status": "ok",
            "ledger": ledger_health,
            "grading": grading_health,
            "q15_v9_5": q15_v95_health,
            "q15_v9_1": q15_v95_health,
            "q15_v9_2": q15_v95_health,
            "q15_v9_3": q15_v95_health,
            "q15_v9_4": q15_v95_health,
            "two_prediction_focus": _app.focus_manager.health(),
            "calibrated_edge": _app.calibrated_edge.health(),
            "q15_v7": _app.professional_v7.health(),
            "q15_v9": {
                "version": "q15-v9-edge-proof-alert-reliability",
                "enabled": True, "read_only": True,
                "canonical_economics": True,
                "persistent_telegram_outbox": True,
                "out_of_sample_framework": True,
                "live_parameter_updates": False,
                "telegram": _app.telegram_outbox.health(),
                # Liveness should not rebuild the full OOS report; that remains on the
                # dedicated diagnostics route.
                "resolved_outcome_metrics_available": False,
            },
            "server_started_at": _app.SERVER_STARTED_AT_ISO,
            "uptime_seconds": round(now - _app.SERVER_STARTED_AT),
            "websocket_connected": bool(wsh.get("connected")),
            "websocket_last_message_at": wsh.get("last_message_at"),
            "websocket_book_ages": wsh.get("book_ages"),
            "kalshi_microstructure_history": wsh.get(
                "microstructure_history"
            ),
            "spot_ws": spot_ws_status,
            "spot_depth": spot_depth_status,
            "spot_l3": spot_l3_status,
            "coinbase_adv_l2": coinbase_adv_l2_status,
            "coinbase_adv_l2_snapshot_age_seconds": coinbase_adv_l2_status.get("snapshot_age_seconds"),
            "kraken_l3": kraken_l3_status,
            "settlement_index": settlement_index_status,
            "rti_exact_13m": exact_rti_13m_status,
            "v11_readiness_monitor": v11_readiness_monitor_status,
            "v13_readiness_monitor": v13_readiness_monitor_status,
            "v14_readiness_monitor": v14_readiness_monitor_status,
            "independent_path_readiness_monitor": (
                independent_path_readiness_monitor_status
            ),
            "rti_path_13m_challenger": rti_path_13m_challenger_status,
            "ladder_probe": ladder_probe_status,
            "market_activity": market_activity_status,
            "path_recorder": path_recorder_status,
            "path_forecast": path_forecast_status,
            "liq_feed": liq_feed_status,
            "strangle_shadow": strangle_shadow_status,
            "cycle_watchdog": _app.cycle_watchdog.health(),
            "feed_watchdog": _app.cycle_watchdog.feed_health(),
            "heartbeat_watchdog": _app.cycle_watchdog.heartbeat_status(now=now),
            "github_relay": _github_relay_status(),
            "startup_config_manifest": _app.startup_config_manifest.health(),
            "current_market_window": current_window,
            "assets_subscribed": [s.get("asset") for s in live],
            "assets_tracked": len(snaps),
            "telegram_status": _app.notifier.status(),
            "model_accuracy": _app.checkpoint_v95.accuracy_summary(),
            "data_age_seconds": data_age,
            "alerts_generated": _app.store.count_alerts(),
            "deployment_type": deployment_type,
            "mode": "ws-primary" if wsh.get("connected") else "rest-polling",
            "persistence": "postgres" if _app.store.enabled else "disabled",
            "last_successful_cycle_age_s": round(now - _app._last_cycle_ok, 2) if _app._last_cycle_ok else None,
            "q15_settings": _app.asdict(_app.upgrade.settings),
            "learning_v4": _app.learner.health_summary(),
            "config": _app.config.as_dict(),
            "health_cache": {
                **capture_guard,
                "served_cached": False,
                "reason": None,
                "cached_at": now,
                "cache_age_seconds": 0.0,
            },
        }
        with _health_cache_lock:
            _health_cache = payload
            _health_cache_at = now
        return _app.jsonify(payload)
