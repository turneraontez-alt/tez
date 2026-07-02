"""Flask routes extracted from app.py (Stage 2 refactor, behavior-preserving).

Each module exposes register(flask_app, host): ``host`` is the app module object
(``sys.modules[__name__]`` at the call site — NOT ``import app``, which would
re-execute app.py's wiring when the process runs as ``python3 app.py`` /
``__main__``). Route bodies reference host globals lazily via ``_app.<name>`` so
values rebound after startup (e.g. _last_cycle_ok) stay live. Endpoints keep
their original function names — the frozen route-table test pins this.
"""

_app = None


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
        now = _app.time.time()
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
            from liq_feed import liq_health
            liq_feed_status = liq_health()
        except Exception:
            liq_feed_status = {"enabled": False}

        deployment_type = "reserved-vm" if _app.os.environ.get("REPLIT_DEPLOYMENT") else "development"

        # Surface learning-ledger health at the top level so silent learning-layer
        # degradation is visible without digging into q15_v9_5.ledger. The owner trades
        # off these alerts, so a calibration that has silently fallen back to identity
        # (calibration_unconverged_fallbacks) or a shadow challenger that has stopped
        # learning (shadow_errors / last_shadow_error) must surface here, not just in
        # logs. Never let a ledger hiccup break the health route itself.
        try:
            ls = _app.checkpoint_v95.ledger.status()
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

        return _app.jsonify({
            "status": "ok",
            "ledger": ledger_health,
            "q15_v9_5": _app.checkpoint_v95.health(),
            "q15_v9_1": _app.checkpoint_v95.health(),
            "q15_v9_2": _app.checkpoint_v95.health(),
            "q15_v9_3": _app.checkpoint_v95.health(),
            "q15_v9_4": _app.checkpoint_v95.health(),
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
                "resolved_outcome_metrics_available": bool(_app.oos_v9.out_of_sample_report().get("available")),
            },
            "server_started_at": _app.SERVER_STARTED_AT_ISO,
            "uptime_seconds": round(now - _app.SERVER_STARTED_AT),
            "websocket_connected": bool(wsh.get("connected")),
            "websocket_last_message_at": wsh.get("last_message_at"),
            "websocket_book_ages": wsh.get("book_ages"),
            "spot_ws": spot_ws_status,
            "spot_depth": spot_depth_status,
            "spot_l3": spot_l3_status,
            "coinbase_adv_l2": coinbase_adv_l2_status,
            "kraken_l3": kraken_l3_status,
            "settlement_index": settlement_index_status,
            "ladder_probe": ladder_probe_status,
            "market_activity": market_activity_status,
            "path_recorder": path_recorder_status,
            "liq_feed": liq_feed_status,
            "cycle_watchdog": _app.cycle_watchdog.health(),
            "feed_watchdog": _app.cycle_watchdog.feed_health(),
            "heartbeat_watchdog": _app.cycle_watchdog.heartbeat_status(now=now),
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
        })
