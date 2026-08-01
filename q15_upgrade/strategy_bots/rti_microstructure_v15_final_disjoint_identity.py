"""Static identity for V15's final fully disjoint NON-BTC audit."""
from __future__ import annotations


PROTOCOL_ID = "q15-rti-v15-non-btc-final-disjoint-audit-v1"
PROTOCOL_SHA256 = (
    "04a9dbf0abaf86b9c6e80b7e91efea0dbed8675f55fdb97a231191d04ad32bac"
)
PROTOCOL_RELATIVE_PATH = (
    "config/q15_rti_v15_final_disjoint_audit_protocol.json"
)
SELECTION_RULE = (
    "EARLIEST_60_COMPLETE_V15_WINDOWS_DISJOINT_FROM_ALL_96_"
    "PREVIOUSLY_AUTHORIZED_CLOSE_WINDOWS"
)

POPULATION_LABELS_READ_BEFORE_FREEZE = False
OUTCOME_VALUES_USED_FOR_SELECTION = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
