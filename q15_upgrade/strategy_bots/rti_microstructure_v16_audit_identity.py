"""Immutable identities for V16's sealed, one-shot development audit."""

EVALUATOR_CONTRACT_ID = "q15-rti-v16-development-one-shot-evaluator-v1"
EVALUATOR_CONTRACT_SHA256 = (
    "8975af2373857164e998b58464458c74ef66402c159b50dfa74941ccf69f4c99"
)
EVALUATOR_CONTRACT_RELATIVE_PATH = (
    "config/q15_rti_v16_development_evaluator_contract.json"
)

DEVELOPMENT_SEAL_VERSION = "q15-rti-v16-development-outcome-blind-seal-v1"
DEVELOPMENT_SEAL_SHA256 = (
    "6f496b8486cb52b60c127acf09f1df9da9bf5883eb5300a306cb3263247a43d6"
)
DEVELOPMENT_SEAL_RELATIVE_PATH = (
    "reports/q15_rti_v16_development/"
    "non_btc_transfer-development-240-v1.json"
)

EVALUATOR_VERSION = "q15-rti-v16-development-walk-forward-evaluator-v1"
RUNNER_VERSION = "q15-rti-v16-development-one-shot-runner-v1"
STATE_VERSION = "q15-rti-v16-development-one-shot-state-v1"

CONFIRMATION_PHRASE = "OPEN_V16_DEVELOPMENT_LABELS_ONCE"

OUTCOME_ACCESS_REQUIRES_EXCLUSIVE_RESERVATION = True
BTC_LABELS_FORBIDDEN = True
PAPER_ARTIFACT_ALLOWED = False
NOTIFICATION_ELIGIBLE = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
