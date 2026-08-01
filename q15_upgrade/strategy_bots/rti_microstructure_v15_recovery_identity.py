"""Static identity for V15's disjoint NON-BTC recovery audit."""
from __future__ import annotations


PROTOCOL_ID = "q15-rti-v15-non-btc-disjoint-recovery-audit-v1"
PROTOCOL_SHA256 = (
    "a6d6acef4aed462ee56dde73b36ea2db219fbb659759fba168d9df65e68487ed"
)
PROTOCOL_RELATIVE_PATH = "config/q15_rti_v15_recovery_audit_protocol.json"
SELECTION_RULE = (
    "EARLIEST_60_COMPLETE_V15_WINDOWS_DISJOINT_FROM_ALL_PARENT_PRETEST_CLOSE_TIMES"
)
DEFAULT_SEAL_RELATIVE_PATH = (
    "reports/q15_rti_v15_audit_seals/"
    "non_btc_transfer-disjoint-recovery-60-v1.json"
)
DEFAULT_STATE_RELATIVE_PATH = (
    "reports/q15_rti_v15_recovery_audit_runs/"
    "non_btc_transfer/pretest-reservation.json"
)
DEFAULT_TEST_STATE_RELATIVE_PATH = (
    "reports/q15_rti_v15_recovery_audit_runs/"
    "non_btc_transfer/untouched-test-reservation.json"
)

RECOVERY_POPULATION_LABELS_READ_BEFORE_FREEZE = False
OUTCOME_VALUES_USED_FOR_SELECTION = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
