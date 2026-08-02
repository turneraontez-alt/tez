"""Immutable identities for V18's first prospective manual review."""

AUDIT_CONTRACT_ID = "q15-rti-v18-first-prospective-review-one-shot-v2"
AUDIT_CONTRACT_SHA256 = (
    "b070c89a035737a55ba37132250b308b5b6537fe888750ea9f04d4c4ea35c6d4"
)
AUDIT_CONTRACT_RELATIVE_PATH = "config/q15_rti_v18_first_review_contract.json"

PROSPECTIVE_SEAL_VERSION = "q15-rti-v18-first-prospective-feature-seal-v2"
PROSPECTIVE_SEAL_RELATIVE_PATH = (
    "reports/q15_rti_v18_prospective/"
    "non_btc_transfer-first-review-v2.json"
)

EVALUATOR_VERSION = "q15-rti-v18-first-prospective-evaluator-v2"
RUNNER_VERSION = "q15-rti-v18-first-prospective-one-shot-runner-v2"
STATE_VERSION = "q15-rti-v18-first-prospective-one-shot-state-v2"
CONFIRMATION_PHRASE = "OPEN_V18_FIRST_PROSPECTIVE_REVIEW_LABELS_ONCE"

OUTCOME_ACCESS_REQUIRES_EXCLUSIVE_RESERVATION = True
BTC_LABELS_FORBIDDEN = True
PAPER_ARTIFACT_ALLOWED = False
NOTIFICATION_ELIGIBLE = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
