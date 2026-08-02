"""Immutable identities for V17's sealed, one-shot development audit."""

EVALUATOR_CONTRACT_ID = "q15-rti-v17-development-one-shot-evaluator-v1"
EVALUATOR_CONTRACT_SHA256 = (
    "449b354ff4f179c1d14810c0bbef9f6ca178ecc40fe3d7e33243a36567b405cb"
)
EVALUATOR_CONTRACT_RELATIVE_PATH = (
    "config/q15_rti_v17_development_evaluator_contract.json"
)

DEVELOPMENT_SEAL_VERSION = "q15-rti-v17-development-outcome-blind-seal-v1"
DEVELOPMENT_SEAL_SHA256 = (
    "dd1d24a7e04b941b9672d48738898037ebc0007901cd6a017b5c18415ac8322d"
)
DEVELOPMENT_SEAL_RELATIVE_PATH = (
    "reports/q15_rti_v17_development/"
    "non_btc_transfer-development-240-v1.json"
)

EVALUATOR_VERSION = "q15-rti-v17-development-walk-forward-evaluator-v1"
RUNNER_VERSION = "q15-rti-v17-development-one-shot-runner-v1"
STATE_VERSION = "q15-rti-v17-development-one-shot-state-v1"

CONFIRMATION_PHRASE = "OPEN_V17_DEVELOPMENT_LABELS_ONCE"

OUTCOME_ACCESS_REQUIRES_EXCLUSIVE_RESERVATION = True
BTC_LABELS_FORBIDDEN = True
PAPER_ARTIFACT_ALLOWED = False
NOTIFICATION_ELIGIBLE = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
