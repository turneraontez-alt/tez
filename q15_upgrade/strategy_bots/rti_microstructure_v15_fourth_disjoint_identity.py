"""Static identity for V15's fourth fully disjoint NON-BTC audit."""
from __future__ import annotations


PROTOCOL_ID = "q15-rti-v15-non-btc-fourth-disjoint-audit-v1"
PROTOCOL_SHA256 = (
    "baad00d291d83d4992059fd112eb2925b93ac384fb4ad460981dd14a6bb401db"
)
PROTOCOL_RELATIVE_PATH = (
    "config/q15_rti_v15_fourth_disjoint_audit_protocol.json"
)
SELECTION_RULE = (
    "EARLIEST_60_COMPLETE_V15_WINDOWS_DISJOINT_FROM_ALL_144_"
    "PREVIOUSLY_AUTHORIZED_CLOSE_WINDOWS"
)

POPULATION_LABELS_READ_BEFORE_FREEZE = False
OUTCOME_VALUES_USED_FOR_SELECTION = False
AUTOMATIC_PROMOTION = False
REAL_TRADING_ALLOWED = False
