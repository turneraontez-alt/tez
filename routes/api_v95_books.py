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

    @flask_app.route("/api/q15-v9-5/diagnostics")
    def q15_v95_diagnostics_ep():
        return _app.jsonify(_app.checkpoint_v95.diagnostics())

    @flask_app.route("/api/q15-v9-5/predictions")
    def q15_v95_predictions_ep():
        return _app.jsonify(_app.checkpoint_v95.predictions())

    @flask_app.route("/api/q15-v9-5/market-data")
    def q15_v95_market_data_ep():
        return _app.jsonify(_app.checkpoint_v95.market_data_status())

    @flask_app.route("/api/q15-v9-5/calibration")
    def q15_v95_calibration_ep():
        return _app.jsonify(_app.checkpoint_v95.calibration_status())

    @flask_app.route("/api/q15-v9-5/learning")
    def q15_v95_learning_ep():
        return _app.jsonify(_app.checkpoint_v95.learning_status())

    @flask_app.route("/api/q15-v9-5/decision-stats")
    def q15_v95_decision_stats_ep():
        return _app.jsonify(_app.checkpoint_v95.decision_stats())

    @flask_app.route("/api/q15-v9-5/scoreboard")
    @flask_app.route("/data/q15-v9-5/scoreboard")
    def q15_v95_scoreboard_ep():
        return _app.jsonify(_app.checkpoint_v95.scoreboard())

    @flask_app.route("/api/q15-v9-5/accuracy")
    @flask_app.route("/data/q15-v9-5/accuracy")
    def q15_v95_accuracy_ep():
        return _app.jsonify(_app.checkpoint_v95.accuracy_report())

    @flask_app.route("/api/q15-v9-5/shadow-signals")
    @flask_app.route("/data/q15-v9-5/shadow-signals")
    def q15_v95_shadow_signals_ep():
        return _app.jsonify(_app.checkpoint_v95.shadow_signal_experiment())

    @flask_app.route("/api/q15-hvf/scoreboard")
    @flask_app.route("/data/q15-hvf/scoreboard")
    def q15_hvf_scoreboard_ep():
        try:
            from q15_upgrade.high_vol_flip.runner import get_runner as _hvf_runner
            runner = _hvf_runner()
            if runner is None:
                return _app.jsonify({"available": False, "enabled": False, "model_version": "high-vol-flip-v1"})
            return _app.jsonify(runner.scoreboard())
        except Exception as exc:
            return _app.jsonify({"available": False, "error": str(exc)})

    @flask_app.route("/api/q15-v3/scoreboard")
    @flask_app.route("/data/q15-v3/scoreboard")
    def q15_v3_scoreboard_ep():
        try:
            from q15_upgrade.strategy_bots import runtime as _v3_runtime
            return _app.jsonify(_v3_runtime.scoreboard())
        except Exception as exc:
            return _app.jsonify({"available": False, "error": str(exc)})
