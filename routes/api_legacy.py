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

    @flask_app.route("/api/q15-v9-4/unified/predictions")
    def q15_v94_unified_predictions_ep():
        return _app.jsonify(_app.checkpoint_v95.unified_predictions())

    @flask_app.route("/api/q15-v9-4/unified/learning")
    def q15_v94_unified_learning_ep():
        return _app.jsonify(_app.checkpoint_v95.unified_learning_status())

    @flask_app.route("/api/q15-v9-4/unified/decision-stats")
    def q15_v94_unified_decision_stats_ep():
        return _app.jsonify(_app.checkpoint_v95.decision_stats())

    @flask_app.route("/api/q15-v9-4/diagnostics")
    def q15_v94_diagnostics_ep():
        return _app.jsonify(_app.checkpoint_v95.diagnostics())

    @flask_app.route("/api/q15-v9-4/context")
    def q15_v94_context_ep():
        return _app.jsonify(_app.checkpoint_v95.context_status())

    @flask_app.route("/api/q15-v9-4/decision-stats")
    def q15_v94_decision_stats_ep():
        return _app.jsonify(_app.checkpoint_v95.decision_stats())

    @flask_app.route("/api/q15-v9-3/diagnostics")
    def q15_v93_diagnostics_ep():
        return _app.jsonify(_app.checkpoint_v95.diagnostics())

    @flask_app.route("/api/q15-v9-3/wick-analysis")
    def q15_v93_wick_analysis_ep():
        return _app.jsonify(_app.checkpoint_v95.wick_status())

    @flask_app.route("/api/q15-v9-3/decision-stats")
    def q15_v93_decision_stats_ep():
        return _app.jsonify(_app.checkpoint_v95.decision_stats())

    @flask_app.route("/api/q15-v9-2/diagnostics")
    def q15_v92_diagnostics_ep():
        return _app.jsonify(_app.checkpoint_v95.diagnostics())

    @flask_app.route("/api/q15-v9-2/consensus")
    def q15_v92_consensus_ep():
        return _app.jsonify(_app.checkpoint_v95.consensus_status())

    @flask_app.route("/api/q15-v9-2/decision-stats")
    def q15_v92_decision_stats_ep():
        return _app.jsonify(_app.checkpoint_v95.decision_stats())

    @flask_app.route("/api/q15-v9-1/diagnostics")
    def q15_v91_diagnostics_ep():
        return _app.jsonify(_app.checkpoint_v95.diagnostics())

    @flask_app.route("/api/q15-v9-1/rolling")
    def q15_v91_rolling_ep():
        return _app.jsonify(_app.checkpoint_v95.rolling_status())

    @flask_app.route("/api/q15-v9-1/predictions")
    def q15_v91_predictions_ep():
        return _app.jsonify(_app.checkpoint_v95.predictions_status())

    @flask_app.route("/api/q15-v9/backtest")
    def q15_v9_backtest_ep():
        return _app.jsonify(_app.oos_v9.backtest_report())

    @flask_app.route("/api/q15-v9/out-of-sample")
    def q15_v9_oos_ep():
        return _app.jsonify(_app.oos_v9.out_of_sample_report())

    @flask_app.route("/api/q15-v9/model-comparison")
    def q15_v9_model_comparison_ep():
        return _app.jsonify(_app.oos_v9.model_comparison())

    @flask_app.route("/api/q15-v9/telegram-outbox")
    def q15_v9_telegram_outbox_ep():
        return _app.jsonify({"version": "q15-v9-telegram-outbox", "rows": _app.telegram_outbox.rows(100), "read_only": True})

    @flask_app.route("/api/q15-v9/telegram-health")
    def q15_v9_telegram_health_ep():
        return _app.jsonify(_app.telegram_outbox.health())

    @flask_app.route("/api/q15-v9/dead-letters")
    def q15_v9_dead_letters_ep():
        return _app.jsonify({"version": "q15-v9-telegram-outbox", "rows": _app.telegram_outbox.dead_letters(100), "read_only": True})

    @flask_app.route("/api/q15-v9/diagnostics")
    def q15_v9_diagnostics_ep():
        oos = _app.oos_v9.out_of_sample_report()
        return _app.jsonify({
            "version": "q15-v9-edge-proof-alert-reliability",
            "probability_pipeline": "market baseline used once + independent residual",
            "economics_version": "q15-v9-canonical-economics",
            "duplicate_market_anchor_check": "patched",
            "side_preservation": "selected side is used for probability, quote, and economics",
            "checkpoint_sequence": "15M independent; 10M compares with saved 15M",
            "telegram_outbox": _app.telegram_outbox.health(),
            "out_of_sample_metric_availability": bool(oos.get("available")),
            "out_of_sample_reason": oos.get("reason"),
            "read_only": True,
        })

    @flask_app.route("/api/q15-v7/diagnostics")
    @flask_app.route("/data/q15-v7/diagnostics")
    def q15_v7_diagnostics_ep():
        return _app.jsonify(_app.professional_v7.summary())

    @flask_app.route("/api/q15-v7/near-misses")
    def q15_v7_near_misses_ep():
        return _app.jsonify({"version": "q15-v7-professional-liveness", "near_misses": _app.professional_v7.near_misses(50), "read_only": True})

    @flask_app.route("/api/q15-v7/liveness")
    def q15_v7_liveness_ep():
        return _app.jsonify(_app.professional_v7.liveness_diagnosis())

    @flask_app.route("/api/q15-v7/gate-ablation")
    def q15_v7_gate_ablation_ep():
        return _app.jsonify({"version": "q15-v7-professional-liveness", "gate_ablation": _app.professional_v7.gate_ablation(), "read_only": True})

    @flask_app.route("/api/q15-v7/threshold-sensitivity")
    def q15_v7_threshold_sensitivity_ep():
        return _app.jsonify(_app.professional_v7.threshold_sensitivity())

    @flask_app.route("/api/q15-v7/hourly-report")
    def q15_v7_hourly_report_ep():
        return _app.jsonify({"version": "q15-v7-professional-liveness", "message": _app.professional_v7.hourly_report(), "read_only": True})
