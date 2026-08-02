"""Immutable identities for V19's first prospective manual review."""

AUDIT_CONTRACT_ID = "q15-rti-v19-first-prospective-review-one-shot-v1"
AUDIT_CONTRACT_SHA256 = (
    "ecd153b13aad4bd6b322c00f401beef606da7ca6b0cc12cde2fe27dd7d689030"
)
AUDIT_CONTRACT_RELATIVE_PATH = "config/q15_rti_v19_first_review_contract.json"

PROSPECTIVE_SEAL_VERSION = "q15-rti-v19-first-prospective-feature-seal-v1"
PROSPECTIVE_SEAL_RELATIVE_PATH = (
    "reports/q15_rti_v19_prospective/"
    "non_btc_transfer-first-review-v1.json"
)

EVALUATOR_VERSION = "q15-rti-v19-first-prospective-evaluator-v1"
RUNNER_VERSION = "q15-rti-v19-first-prospective-one-shot-runner-v1"
STATE_VERSION = "q15-rti-v19-first-prospective-one-shot-state-v1"
CONFIRMATION_PHRASE = "OPEN_V19_FIRST_PROSPECTIVE_REVIEW_LABELS_ONCE"

OUTCOME_ACCESS_REQUIRES_EXCLUSIVE_RESERVATION = True
BTC_LABELS_FORBIDDEN = True
PAPER_ARTIFACT_ALLOWED = False
NOTIFICATION_ELIGIBLE = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
