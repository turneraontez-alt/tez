"""Static identity for V21's outcome-blind prospective PAPER contract."""

PROTOCOL_ID = "q15-rti-v21-prospective-paper-deployment-and-review-v1"
PROTOCOL_SHA256 = (
    "81065754fa45ddbccc2a535e7be3327d9e175bf1756b98bd6356a446cad53e66"
)
PROTOCOL_RELATIVE_PATH = "config/q15_rti_v21_paper_deployment_protocol.json"
ARTIFACT_VERSION = "q15-rti-v21-paper-model-artifact-v1"
LEDGER_VERSION = "q15-rti-v21-prospective-paper-ledger-v1"
DEFAULT_LEDGER_RELATIVE_PATHS = {
    "NON_BTC_TRANSFER": "data/q15_rti_v21_paper_non_btc_transfer_v1.sqlite3",
    "BTC": "data/q15_rti_v21_paper_btc_v1.sqlite3",
}

PROTOCOL_FROZEN = True
V21_ELIGIBLE_ROWS_BEFORE_FREEZE = 0
OUTCOME_LABELS_USED_FOR_PROTOCOL = False
PAPER_ARTIFACT_CREATED = False
RUNTIME_SCORING_CONNECTED = False
NOTIFICATIONS_ENABLED = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
