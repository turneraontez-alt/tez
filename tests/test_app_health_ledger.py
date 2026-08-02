"""/api/health surfaces learning-ledger health at the top level.

The review flagged that when the ledger is unavailable, calibration silently
falls back to identity while health only showed it buried in q15_v9_5.ledger.
This asserts a compact top-level `ledger` block (with an `available` flag) so an
operator can see "ledger down" at a glance, and that a ledger error never breaks
the health route itself.
"""
from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class TestHealthLedgerSurface(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    def test_health_includes_top_level_ledger_block(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("ledger", body)
        self.assertIn("available", body["ledger"])
        self.assertIsInstance(body["ledger"]["available"], bool)

    def test_health_exposes_nonblocking_drift_reconcile_worker(self):
        body = self.client.get("/api/health").get_json()
        worker = body["drift_delivery_reconcile"]
        self.assertIn("inflight", worker)
        self.assertFalse(worker["live_refresh_loop_blocking_allowed"])

    def test_health_exposes_v13_admin_monitor_without_trade_authority(self):
        body = self.client.get("/api/health").get_json()
        monitor = body["v13_readiness_monitor"]
        self.assertTrue(monitor["paper_only"])
        self.assertTrue(monitor["administrative_notices_only"])
        self.assertFalse(monitor["notification_is_trade_signal"])
        self.assertFalse(monitor["outcome_labels_read"])
        self.assertFalse(monitor["automatic_scoring"])
        self.assertFalse(monitor["automatic_promotion"])
        self.assertFalse(monitor["real_trading_allowed"])

    def test_health_exposes_v14_admin_monitor_without_trade_authority(self):
        body = self.client.get("/api/health").get_json()
        monitor = body["v14_readiness_monitor"]
        self.assertTrue(monitor["paper_only"])
        self.assertTrue(monitor["administrative_notices_only"])
        self.assertFalse(monitor["notification_is_trade_signal"])
        self.assertFalse(monitor["outcome_labels_read"])
        self.assertFalse(monitor["automatic_scoring"])
        self.assertFalse(monitor["automatic_promotion"])
        self.assertFalse(monitor["real_trading_allowed"])

    def test_health_exposes_path_admin_monitor_without_predictive_authority(self):
        body = self.client.get("/api/health").get_json()
        monitor = body["independent_path_readiness_monitor"]
        self.assertTrue(monitor["paper_only"])
        self.assertTrue(monitor["administrative_notices_only"])
        self.assertFalse(monitor["notification_is_trade_signal"])
        self.assertFalse(monitor["outcome_labels_read"])
        self.assertFalse(monitor["automatic_scoring"])
        self.assertFalse(monitor["automatic_promotion"])
        self.assertFalse(monitor["feature_selection_performed"])
        self.assertFalse(monitor["thresholds_selected_from_outcomes"])
        self.assertFalse(monitor["real_trading_allowed"])
        self.assertTrue(monitor["capture_protection_enabled"])
        self.assertEqual(monitor["capture_protected_before_seconds"], 130)
        self.assertEqual(monitor["capture_protected_after_seconds"], 5)
        self.assertEqual(
            monitor["degradation_notice_policy_id"],
            "q15-rti-independent-path-prospective-degradation-notice-v1",
        )
        self.assertEqual(len(monitor["degradation_notice_policy_sha256"]), 64)
        self.assertEqual(
            monitor["degradation_notice_first_eligible_close_time"],
            1784763900.0,
        )
        self.assertEqual(
            monitor["degradation_notice_evaluation_grace_seconds"], 5
        )
        self.assertEqual(monitor["entirely_missing_due_close_count"], 0)
        self.assertEqual(monitor["prospective_degradation_event_count"], 0)
        self.assertEqual(monitor["completed_degradation_close_times"], [])
        self.assertEqual(
            monitor["successor_charter_id"],
            "q15-rti-v15-independent-path-augmented-nested-safe-residual-preregistration-v1",
        )
        self.assertEqual(len(monitor["successor_charter_sha256"]), 64)
        self.assertEqual(len(monitor["successor_evaluation_protocol_sha256"]), 64)
        self.assertTrue(monitor["successor_executable_design_created"])
        self.assertEqual(
            monitor["successor_executable_design_id"],
            "q15-rti-market-anchored-independent-path-augmented-residual-v15",
        )
        self.assertEqual(
            len(monitor["successor_executable_design_sha256"]), 64
        )
        self.assertTrue(
            monitor["successor_runtime_feature_construction_connected"]
        )
        self.assertFalse(monitor["successor_outcome_access_allowed"])
        self.assertFalse(monitor["successor_model_fit_performed"])
        self.assertFalse(monitor["successor_runtime_scoring_connected"])
        self.assertTrue(monitor["successor_historical_audit_tooling_ready"])
        self.assertLessEqual(
            monitor["successor_audit_complete_close_windows"],
            monitor["complete_reconstructable_close_windows"],
        )
        self.assertEqual(
            monitor["successor_audit_complete_rows"],
            monitor["successor_audit_complete_close_windows"] * 7,
        )
        self.assertEqual(
            monitor["successor_audit_feature_ineligible_source_windows"],
            monitor["complete_reconstructable_close_windows"]
            - monitor["successor_audit_complete_close_windows"],
        )
        self.assertFalse(
            monitor["successor_audit_population_outcome_labels_read"]
        )
        self.assertFalse(
            monitor["successor_audit_population_model_fit_performed"]
        )
        self.assertEqual(
            monitor["successor_audit_seal_version"],
            "q15-rti-v15-outcome-blind-audit-execution-seal-v3",
        )
        self.assertEqual(
            monitor["successor_walk_forward_evaluator_version"],
            "q15-rti-v15-paired-comparator-walk-forward-v2",
        )
        self.assertEqual(
            monitor["successor_pretest_runner_version"],
            "q15-rti-v15-durable-one-shot-train-calibration-v4",
        )
        self.assertEqual(
            monitor["successor_pretest_state_version"],
            "q15-rti-v15-append-only-train-calibration-state-v4",
        )
        self.assertEqual(
            monitor["successor_untouched_test_runner_version"],
            "q15-rti-v15-durable-one-shot-untouched-test-v4",
        )
        self.assertEqual(
            monitor["successor_untouched_test_state_version"],
            "q15-rti-v15-append-only-untouched-test-state-v4",
        )
        self.assertEqual(
            monitor["successor_settlement_evidence_version"],
            "q15-rti-v15-authoritative-kalshi-settlement-verification-v2",
        )
        self.assertEqual(
            monitor["successor_settlement_evidence_source_id"],
            "KALSHI_PUBLIC_MARKET_API",
        )
        self.assertTrue(monitor["successor_paper_protocol_frozen"])
        self.assertEqual(
            monitor["successor_paper_protocol_id"],
            "q15-rti-v15-prospective-paper-deployment-and-review-v1",
        )
        self.assertEqual(
            monitor["successor_paper_protocol_sha256"],
            "b4ae8b458a5241c289d03d32186fec8ff2f6b6247d00323cca18863a40f82175",
        )
        self.assertEqual(
            monitor["successor_paper_artifact_version"],
            "q15-rti-v15-paper-model-artifact-v1",
        )
        self.assertEqual(
            monitor["successor_paper_ledger_version"],
            "q15-rti-v15-prospective-paper-ledger-v1",
        )
        self.assertFalse(monitor["successor_paper_artifact_created"])
        self.assertFalse(
            monitor["successor_paper_runtime_scoring_connected"]
        )
        self.assertFalse(monitor["successor_paper_notifications_enabled"])
        self.assertFalse(monitor["successor_paper_automatic_promotion"])
        self.assertFalse(monitor["successor_paper_real_trading_allowed"])
        self.assertEqual(
            len(monitor["successor_reporting_protocol_sha256"]), 64
        )
        self.assertFalse(monitor["successor_notification_eligible"])
        self.assertFalse(monitor["successor_automatic_promotion"])
        self.assertFalse(monitor["successor_real_trading_allowed"])
        self.assertEqual(
            monitor["geometry_freeze_contract_id"],
            "q15-rti-independent-path-geometry-30-immutable-freeze-v1",
        )
        self.assertEqual(len(monitor["geometry_freeze_contract_sha256"]), 64)
        self.assertTrue(monitor["geometry_freeze_manual_command_only"])
        self.assertFalse(monitor["geometry_freeze_background_write_allowed"])

    def test_health_uses_cached_snapshot_during_exact_path_collection(self):
        from types import SimpleNamespace
        from routes import api_core

        original_time = appmod.time
        original_started = appmod._refresh_started
        original_market_health = appmod.market_data.health
        with api_core._health_cache_lock:
            original_cache = api_core._health_cache
            original_cache_at = api_core._health_cache_at
            api_core._health_cache = {"status": "ok", "sentinel": "cached"}
            api_core._health_cache_at = 1784760390.0
        try:
            appmod.time = SimpleNamespace(time=lambda: 1784760400.0)
            appmod._refresh_started = True

            def forbidden_health():
                raise AssertionError("full health graph ran inside capture guard")

            appmod.market_data.health = forbidden_health
            body = self.client.get("/api/health").get_json()
            self.assertEqual(body["sentinel"], "cached")
            self.assertTrue(body["health_cache"]["served_cached"])
            self.assertTrue(body["health_cache"]["protected"])
            self.assertEqual(
                body["health_cache"]["reason"],
                "EXACT_INDEPENDENT_PATH_COLLECTION_GUARD",
            )
        finally:
            appmod.time = original_time
            appmod._refresh_started = original_started
            appmod.market_data.health = original_market_health
            with api_core._health_cache_lock:
                api_core._health_cache = original_cache
                api_core._health_cache_at = original_cache_at

    def test_live_health_uses_bounded_overlay_outside_capture_guard(self):
        from types import SimpleNamespace
        from routes import api_core

        original_time = appmod.time
        original_started = appmod._refresh_started
        original_market_health = appmod.market_data.health
        with api_core._health_cache_lock:
            original_cache = api_core._health_cache
            original_cache_at = api_core._health_cache_at
            api_core._health_cache = {
                "status": "ok",
                "sentinel": "cached-expensive-diagnostics",
            }
            api_core._health_cache_at = 200.0
        try:
            # Phase 221 is one second beyond the protected +100s boundary.
            appmod.time = SimpleNamespace(time=lambda: 221.0)
            appmod._refresh_started = True
            appmod.market_data.health = lambda: {
                "connected": True,
                "last_message_at": 220.5,
                "book_ages": {"TEST": 0.5},
                "microstructure_history": {"buffers": {}},
            }
            body = self.client.get("/api/health").get_json()
            self.assertEqual(
                body["sentinel"], "cached-expensive-diagnostics"
            )
            self.assertTrue(body["websocket_connected"])
            self.assertTrue(body["health_cache"]["served_cached"])
            self.assertFalse(body["health_cache"]["protected"])
            self.assertEqual(
                body["health_cache"]["reason"],
                "LIVE_NONBLOCKING_HEALTH",
            )
            self.assertEqual(body["health_cache"]["cache_age_seconds"], 21.0)
            self.assertEqual(body["health_cache"]["live_overlay_updated_at"], 221.0)
        finally:
            appmod.time = original_time
            appmod._refresh_started = original_started
            appmod.market_data.health = original_market_health
            with api_core._health_cache_lock:
                api_core._health_cache = original_cache
                api_core._health_cache_at = original_cache_at

    def test_exact_capture_health_guard_boundaries_are_deterministic(self):
        from routes.api_core import _exact_capture_guard_state

        # Capture phase is 120 modulo 900.
        self.assertTrue(_exact_capture_guard_state(45.0)["protected"])
        self.assertFalse(_exact_capture_guard_state(44.0)["protected"])
        self.assertTrue(_exact_capture_guard_state(120.0)["protected"])
        self.assertTrue(_exact_capture_guard_state(125.0)["protected"])
        self.assertTrue(_exact_capture_guard_state(220.0)["protected"])
        self.assertFalse(_exact_capture_guard_state(221.0)["protected"])

    def test_unavailable_ledger_is_visible_at_top_level(self):
        # When the ledger is down it returns an unavailable status (calibration
        # then falls back to identity); the top-level block must reflect that so
        # the degradation is not hidden in q15_v9_5.ledger.
        orig = appmod.checkpoint_v95.ledger.status
        appmod.checkpoint_v95.ledger.status = lambda: {
            "available": False, "path": "/data/x.db", "error": "ledger offline",
        }
        try:
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertFalse(body["ledger"]["available"])
            self.assertEqual(body["ledger"]["error"], "ledger offline")
        finally:
            appmod.checkpoint_v95.ledger.status = orig

    def test_health_includes_grading_backlog_block(self):
        orig = appmod.checkpoint_v95.ledger.reconcile_backlog_status

        def fake_grading(*, now=None):
            return {
                "available": True,
                "resolved_24h": 7,
                "unresolved_pastclose": 42,
                "oldest_unresolved_age_seconds": 900,
                "newest_resolved_at": 123.0,
                "newest_resolved_age_seconds": 60,
                "parked": 3,
            }

        appmod.checkpoint_v95.ledger.reconcile_backlog_status = fake_grading
        try:
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["grading"]["resolved_24h"], 7)
            self.assertEqual(body["grading"]["unresolved_pastclose"], 42)
            self.assertEqual(body["grading"]["parked"], 3)
            self.assertEqual(body["q15_v9_5"]["grading"]["resolved_24h"], 7)
        finally:
            appmod.checkpoint_v95.ledger.reconcile_backlog_status = orig

    def test_health_uses_compact_v95_block(self):
        orig = appmod.checkpoint_v95.health

        def full_health_should_not_run():
            raise AssertionError("full V9.5 diagnostic health should not run on /api/health")

        appmod.checkpoint_v95.health = full_health_should_not_run
        try:
            resp = self.client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["q15_v9_5"]["version"], appmod.checkpoint_v95.VERSION)
            self.assertNotIn("parent_v94", body["q15_v9_5"])
        finally:
            appmod.checkpoint_v95.health = orig

    def test_health_includes_coinbase_adv_l2_snapshot_age_alias(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("coinbase_adv_l2", body)
        self.assertIn("coinbase_adv_l2_snapshot_age_seconds", body)

    def test_health_includes_unpromoted_rti_challenger_status(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("rti_path_13m_challenger", body)
        challenger = body["rti_path_13m_challenger"]
        self.assertTrue(challenger["paper_only"])
        self.assertEqual(challenger["id"], "impulse_strength_v1")
        self.assertTrue(challenger["notification_eligible"])
        self.assertFalse(challenger["historical_credit_allowed"])
        self.assertFalse(challenger.get("automatic_promotion", False))
        probability_model = challenger["probability_model"]
        self.assertTrue(probability_model["paper_only"])
        self.assertFalse(probability_model["notification_eligible"])
        self.assertFalse(probability_model["automatic_promotion"])
        self.assertIn("prospective_after_close_time", probability_model)
        probability_models = challenger["probability_models"]
        self.assertIn("v2_quarantined_control", probability_models)
        self.assertIn("v3_challenger", probability_models)
        self.assertFalse(
            probability_models["v3_challenger"]["notification_eligible"]
        )
        self.assertFalse(
            probability_models["v3_challenger"]["promotion_eligible"]
        )
        v11_locked_artifacts = challenger["v11_locked_artifacts"]
        self.assertEqual(
            set(v11_locked_artifacts), {"BTC", "NON_BTC_TRANSFER"}
        )
        for cohort, artifact_health in v11_locked_artifacts.items():
            self.assertEqual(artifact_health["cohort"], cohort)
            self.assertTrue(artifact_health["paper_only"])
            self.assertFalse(artifact_health["notification_eligible"])
            self.assertFalse(artifact_health["automatic_promotion"])
            self.assertFalse(artifact_health["real_trading_allowed"])
            self.assertTrue(artifact_health["manual_activation_required"])
            self.assertFalse(artifact_health["paper_record_enabled"])
            self.assertEqual(
                artifact_health["prospective_ledger_status"],
                "DISABLED_MANUAL_ACTIVATION_REQUIRED",
            )
            self.assertFalse(
                artifact_health["prospective_ledger_notification_eligible"]
            )
        v12_locked_artifacts = challenger["v12_locked_artifacts"]
        self.assertEqual(
            set(v12_locked_artifacts), {"BTC", "NON_BTC_TRANSFER"}
        )
        for cohort, artifact_health in v12_locked_artifacts.items():
            self.assertEqual(artifact_health["cohort"], cohort)
            self.assertEqual(
                artifact_health["design_id"],
                "q15-rti-market-residual-orthogonal-compact-v12",
            )
            self.assertTrue(artifact_health["paper_only"])
            self.assertFalse(artifact_health["notification_eligible"])
            self.assertFalse(artifact_health["automatic_promotion"])
            self.assertFalse(artifact_health["real_trading_allowed"])
            self.assertTrue(artifact_health["manual_activation_required"])
            self.assertFalse(artifact_health["paper_record_enabled"])
            self.assertFalse(artifact_health["runtime_scoring_connected"])
            self.assertTrue(artifact_health["artifact_installation_manual"])
            self.assertEqual(
                artifact_health["prospective_ledger_status"],
                "DISABLED_COLLECTION_AND_TEST_GATES_REQUIRED",
            )
            self.assertFalse(
                artifact_health["prospective_ledger_notification_eligible"]
            )
        v11_readiness = challenger["v11_collection_readiness"]
        self.assertTrue(v11_readiness["available"])
        self.assertEqual(
            v11_readiness["design_id"],
            "q15-rti-market-residual-cross-asset-regime-v11",
        )
        self.assertEqual(v11_readiness["feature_count"], 71)
        self.assertGreaterEqual(
            v11_readiness["complete_executable_close_windows"], 0
        )
        self.assertIn(
            "windows_remaining_to_first_feature_review", v11_readiness
        )
        self.assertTrue(v11_readiness["paper_only"])
        self.assertFalse(v11_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(v11_readiness["notification_eligible"])
        self.assertFalse(v11_readiness["real_trading_allowed"])
        v12_readiness = challenger["v12_collection_readiness"]
        self.assertTrue(v12_readiness["available"])
        self.assertEqual(
            v12_readiness["design_id"],
            "q15-rti-market-residual-orthogonal-compact-v12",
        )
        self.assertEqual(v12_readiness["feature_count"], 20)
        self.assertTrue(v12_readiness["v11_remains_frozen_parallel_control"])
        self.assertTrue(v12_readiness["paper_only"])
        self.assertFalse(v12_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(v12_readiness["notification_eligible"])
        self.assertFalse(v12_readiness["real_trading_allowed"])
        v13_readiness = challenger["v13_collection_readiness"]
        self.assertTrue(v13_readiness["available"])
        self.assertEqual(
            v13_readiness["design_id"],
            "q15-rti-market-residual-cohort-conditioned-compact-v13",
        )
        self.assertEqual(v13_readiness["feature_count"], 20)
        self.assertTrue(
            v13_readiness["v11_and_v12_remain_frozen_parallel_controls"]
        )
        self.assertFalse(v13_readiness["runtime_scoring_connected"])
        self.assertFalse(v13_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(v13_readiness["notification_eligible"])
        self.assertFalse(v13_readiness["real_trading_allowed"])
        self.assertEqual(v13_readiness["geometry_review_windows"], 30)
        self.assertEqual(v13_readiness["covariate_drift_review_windows"], 60)
        self.assertEqual(
            len(v13_readiness["geometry_review_protocol_sha256"]), 64
        )
        self.assertEqual(
            len(v13_readiness["covariate_drift_protocol_sha256"]), 64
        )
        self.assertEqual(
            len(v13_readiness["subgroup_reporting_protocol_sha256"]), 64
        )
        self.assertEqual(
            len(v13_readiness["calibration_reporting_protocol_sha256"]), 64
        )
        self.assertEqual(
            len(v13_readiness["selective_value_curve_protocol_sha256"]), 64
        )
        self.assertFalse(
            v13_readiness["performance_reporting_outcome_labels_read"]
        )
        self.assertFalse(
            v13_readiness["performance_reporting_changes_deployment_gate"]
        )
        self.assertIn(
            "performance_skill_gate",
            probability_models["v3_challenger"],
        )
        probability_scorecards = challenger["probability_scorecards"]
        self.assertIn("rti_probability_value_v2", probability_scorecards)
        self.assertIn("rti_probability_value_v3", probability_scorecards)
        self.assertIn("rti_microstructure_value_v11", probability_scorecards)
        self.assertTrue(
            probability_scorecards["rti_probability_value_v2"][
                "promotion_prohibited"
            ]
        )
        self.assertFalse(
            probability_scorecards["rti_probability_value_v3"][
                "accepted_trade_filter_applied"
            ]
        )
        feature_coverage = challenger["exact_feature_coverage"]
        self.assertIn("kalshi_microstructure_history", body)
        self.assertIn("model_feature_v1", feature_coverage)
        self.assertIn("model_feature_v2", feature_coverage)
        self.assertIn("model_feature_v3", feature_coverage)
        self.assertIn("model_feature_v4", feature_coverage)
        self.assertIn("model_feature_v5", feature_coverage)
        self.assertIn("model_feature_v6", feature_coverage)
        self.assertIn("model_feature_v7", feature_coverage)
        self.assertIn("model_feature_v8", feature_coverage)
        self.assertIn("model_feature_v9", feature_coverage)
        self.assertIn("dynamics_extension_v1", feature_coverage)
        self.assertFalse(
            feature_coverage["dynamics_extension_v1"][
                "outcome_labels_read"
            ]
        )
        self.assertFalse(
            feature_coverage["dynamics_extension_v1"][
                "model_fit_performed"
            ]
        )
        self.assertTrue(
            feature_coverage["model_feature_v4"][
                "primary_preregistered_design"
            ]
        )
        self.assertFalse(
            feature_coverage["model_feature_v5"]["next_preregistered_design"]
        )
        self.assertFalse(
            feature_coverage["model_feature_v6"]["next_preregistered_design"]
        )
        self.assertFalse(
            feature_coverage["model_feature_v7"]["next_preregistered_design"]
        )
        self.assertFalse(
            feature_coverage["model_feature_v8"]["next_preregistered_design"]
        )
        self.assertFalse(
            feature_coverage["model_feature_v11"]["next_preregistered_design"]
        )
        self.assertFalse(
            feature_coverage["model_feature_v12"]["next_preregistered_design"]
        )
        self.assertTrue(
            feature_coverage["model_feature_v13"]["next_preregistered_design"]
        )
        dynamics_readiness = feature_coverage[
            "dynamics_v5_model_readiness"
        ]
        self.assertTrue(dynamics_readiness["paper_only"])
        self.assertFalse(dynamics_readiness["notification_eligible"])
        self.assertFalse(dynamics_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(dynamics_readiness["model_fit_performed"])
        self.assertEqual(dynamics_readiness["feature_count"], 46)
        lead_lag_readiness = feature_coverage[
            "lead_lag_v6_model_readiness"
        ]
        self.assertTrue(lead_lag_readiness["paper_only"])
        self.assertFalse(lead_lag_readiness["notification_eligible"])
        self.assertFalse(lead_lag_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(lead_lag_readiness["model_fit_performed"])
        self.assertEqual(lead_lag_readiness["feature_count"], 53)
        cross_venue_readiness = feature_coverage[
            "cross_venue_v7_model_readiness"
        ]
        self.assertTrue(cross_venue_readiness["paper_only"])
        self.assertFalse(cross_venue_readiness["notification_eligible"])
        self.assertFalse(cross_venue_readiness["readiness_uses_outcome_labels"])
        self.assertFalse(cross_venue_readiness["model_fit_performed"])
        self.assertEqual(cross_venue_readiness["feature_count"], 60)
        independent_venue_readiness = feature_coverage[
            "independent_venue_v8_model_readiness"
        ]
        self.assertTrue(independent_venue_readiness["paper_only"])
        self.assertFalse(independent_venue_readiness["notification_eligible"])
        self.assertFalse(
            independent_venue_readiness["readiness_uses_outcome_labels"]
        )
        self.assertFalse(independent_venue_readiness["model_fit_performed"])
        self.assertEqual(independent_venue_readiness["feature_count"], 53)
        independent_microstructure_readiness = feature_coverage[
            "independent_microstructure_v9_model_readiness"
        ]
        self.assertTrue(independent_microstructure_readiness["paper_only"])
        self.assertFalse(
            independent_microstructure_readiness["notification_eligible"]
        )
        self.assertFalse(
            independent_microstructure_readiness["readiness_uses_outcome_labels"]
        )
        self.assertFalse(
            independent_microstructure_readiness["model_fit_performed"]
        )
        self.assertEqual(independent_microstructure_readiness["feature_count"], 65)
        compact_readiness = feature_coverage[
            "independent_microstructure_compact_v10_model_readiness"
        ]
        self.assertEqual(compact_readiness["feature_count"], 63)
        self.assertFalse(compact_readiness["notification_eligible"])
        self.assertFalse(compact_readiness["readiness_uses_outcome_labels"])
        cross_asset_readiness = feature_coverage[
            "cross_asset_regime_v11_model_readiness"
        ]
        self.assertEqual(cross_asset_readiness["feature_count"], 71)
        self.assertFalse(cross_asset_readiness["notification_eligible"])
        self.assertFalse(cross_asset_readiness["readiness_uses_outcome_labels"])
        compact_v12_readiness = feature_coverage[
            "orthogonal_compact_v12_model_readiness"
        ]
        self.assertEqual(compact_v12_readiness["feature_count"], 20)
        self.assertFalse(compact_v12_readiness["notification_eligible"])
        self.assertFalse(
            compact_v12_readiness["readiness_uses_outcome_labels"]
        )
        compact_v13_readiness = feature_coverage[
            "cohort_conditioned_compact_v13_model_readiness"
        ]
        self.assertEqual(compact_v13_readiness["feature_count"], 20)
        self.assertFalse(compact_v13_readiness["notification_eligible"])
        self.assertFalse(
            compact_v13_readiness["readiness_uses_outcome_labels"]
        )
        self.assertTrue(
            compact_v13_readiness[
                "v11_and_v12_remain_frozen_parallel_controls"
            ]
        )
        readiness = feature_coverage["preregistered_model_readiness"]
        self.assertTrue(readiness["paper_only"])
        self.assertFalse(readiness["notification_eligible"])
        self.assertFalse(readiness["real_trading_allowed"])
        self.assertFalse(readiness["readiness_uses_outcome_labels"])
        self.assertFalse(readiness["model_fit_performed"])
        self.assertFalse(readiness["artifact_emitted"])
        self.assertTrue(readiness["timestamp_integrity_clean"])
        self.assertIn("NON_BTC_TRANSFER", readiness["cohorts"])
        self.assertIn("BTC", readiness["cohorts"])

    def test_health_includes_runtime_manifest_and_strangle_shadow(self):
        body = self.client.get("/api/health").get_json()
        self.assertIn("startup_config_manifest", body)
        self.assertIn("ok", body["startup_config_manifest"])
        self.assertIn("strangle_shadow", body)
        self.assertIn("latest_age_seconds", body["strangle_shadow"])

    def test_enabled_empty_collector_gets_watchdog_age_from_startup(self):
        orig_started = appmod.SERVER_STARTED_AT
        appmod.SERVER_STARTED_AT = 100.0
        try:
            self.assertEqual(
                appmod._feed_watchdog_age({"enabled": True, "status": "empty"}, "age", 1000.0),
                900.0,
            )
            self.assertIsNone(
                appmod._feed_watchdog_age({"enabled": False, "status": "disabled"}, "age", 1000.0)
            )
        finally:
            appmod.SERVER_STARTED_AT = orig_started


if __name__ == "__main__":
    unittest.main()
