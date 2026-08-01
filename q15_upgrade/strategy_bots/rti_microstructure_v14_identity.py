"""Immutable identities for the prospective RTI V14 architecture.

V14 was frozen from opened V11 architecture evidence while every V13 outcome
remained unread.  It preserves V13 features and adds only a nested,
training-only residual-trust selector with exact market fallback.
"""

CHARTER_ID = "q15-rti-v14-nested-safe-residual-trust-preregistration-v1"
CHARTER_SHA256 = "30d1d00af4cd6abac5d1775e8e722a39b49cb2849311eea7f304c1b2bd2ec670"
DESIGN_ID = "q15-rti-market-anchored-nested-safe-residual-v14"
DESIGN_SHA256 = "aa5efa9a986dc575ee4e358777cd2394b38550ad7328154b58a7d06bf55c3dda"
EVALUATION_PROTOCOL_ID = (
    "q15-rti-v14-nested-safe-residual-expanding-walk-forward-v1"
)
EVALUATION_PROTOCOL_SHA256 = (
    "638db046f638324b1bcf0459c8362a0f0f12cfef35fe9ebbf7d94dd0add87257"
)
REPORTING_PROTOCOL_ID = "q15-rti-v14-fixed-subgroup-reporting-v1"
REPORTING_PROTOCOL_SHA256 = (
    "88609210e20799933e7b860ee701b47127eb5f799b5b9c2d28ffb90b2c7003eb"
)
CALIBRATION_REPORTING_PROTOCOL_ID = (
    "q15-rti-v14-fixed-calibration-reporting-v1"
)
CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    "72e89f5950b5f70b8603036b0339a35f136421b6051729db7356335c9c6def45"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_ID = (
    "q15-rti-v14-fixed-selective-value-curve-v1"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    "5a5a3a703f73a021a04993c26824ec9e998f7a6aae74deded5e61b889a654fa4"
)

PROSPECTIVE_AFTER_CLOSE_TIME = 1784742300.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784743200.0

EXECUTABLE_DESIGN_FROZEN = True
RUNTIME_SCORING_CONNECTED = False
NOTIFICATION_ELIGIBLE = False
REAL_TRADING_ALLOWED = False
