"""Static identity for V15's manual, fail-closed historical audit tooling."""
from __future__ import annotations


AUDIT_SEAL_VERSION = "q15-rti-v15-outcome-blind-audit-execution-seal-v3"
WALK_FORWARD_EVALUATOR_VERSION = (
    "q15-rti-v15-paired-comparator-walk-forward-v2"
)
PRETEST_RUNNER_VERSION = (
    "q15-rti-v15-durable-one-shot-train-calibration-v4"
)
PRETEST_STATE_VERSION = (
    "q15-rti-v15-append-only-train-calibration-state-v4"
)
UNTOUCHED_TEST_RUNNER_VERSION = (
    "q15-rti-v15-durable-one-shot-untouched-test-v4"
)
UNTOUCHED_TEST_STATE_VERSION = (
    "q15-rti-v15-append-only-untouched-test-state-v4"
)
SETTLEMENT_EVIDENCE_VERSION = (
    "q15-rti-v15-authoritative-kalshi-settlement-verification-v2"
)
SETTLEMENT_EVIDENCE_SOURCE_ID = "KALSHI_PUBLIC_MARKET_API"
REPORTING_PROTOCOL_ID = (
    "q15-rti-v15-fixed-subgroup-and-economics-reporting-v1"
)
REPORTING_PROTOCOL_SHA256 = (
    "57c668865a90be5dc18a301210bff4f77614b3275b414223e8f03aaffa60439d"
)

HISTORICAL_AUDIT_TOOLING_READY = True
AUTOMATIC_OUTCOME_ACCESS = False
AUTOMATIC_MODEL_FIT = False
AUTOMATIC_TEST_SCORING = False
PAPER_ARTIFACT_CREATED = False
NOTIFICATION_ELIGIBLE = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
