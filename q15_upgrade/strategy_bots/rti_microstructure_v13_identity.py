"""Immutable identities for the prospective RTI V13 successor.

The charter was frozen from outcome-blind V12 geometry.  The separate design
and evaluation protocol were then frozen before V13's first eligible close.
None of these identities enables fitting, notification, promotion, or trading.
"""

CHARTER_ID = "q15-rti-v13-btc-alias-successor-preregistration-v1"
CHARTER_SHA256 = "f55e3772f4b6bced8a2315c94d007bf35eac05b38a27391e071d4dd570abae78"
PROPOSED_DESIGN_ID = "q15-rti-market-residual-cohort-conditioned-compact-v13"
DESIGN_ID = PROPOSED_DESIGN_ID
DESIGN_SHA256 = "adc900b5882567446cb3d4a8f5fc0cb795e278dd38db2c6179e54cc83fc673ed"
EVALUATION_PROTOCOL_ID = "q15-rti-v13-expanding-walk-forward-evaluation-v1"
EVALUATION_PROTOCOL_SHA256 = (
    "8abc35d34ca74bb70b2886913648c6ff4189ba9427eed825b79dddc5955b490c"
)
GEOMETRY_REVIEW_PROTOCOL_ID = (
    "q15-rti-v13-outcome-blind-geometry-review-v1"
)
GEOMETRY_REVIEW_PROTOCOL_SHA256 = (
    "550e8dfd3132712020aa90232dab97679cf209a8d5ba5438c6bd7d786b42605b"
)
COVARIATE_DRIFT_PROTOCOL_ID = (
    "q15-rti-v13-outcome-blind-60-window-covariate-drift-v1"
)
COVARIATE_DRIFT_PROTOCOL_SHA256 = (
    "91589996d48ec047b74b5e8c25c4b92533b220dd4a41729cbc71f91aa14a5856"
)
REPORTING_PROTOCOL_ID = "q15-rti-v13-fixed-subgroup-reporting-v1"
REPORTING_PROTOCOL_SHA256 = (
    "ea4a273530cb2a807d091703703d594ec2e7923cfb173fa5acd3de4d13b2a823"
)
CALIBRATION_REPORTING_PROTOCOL_ID = (
    "q15-rti-v13-fixed-calibration-reporting-v1"
)
CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    "cc7e8ddcca5d797d6a1407d3acc0b4d20eba02ee7d8897f9af6c346f6a1120ce"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_ID = (
    "q15-rti-v13-fixed-selective-value-curve-v1"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    "848c0155bcae020c3daf1d7dc34cab2d3663b2f32ceca909b5be05e1f05a25e7"
)
CHARTER_PROSPECTIVE_AFTER_CLOSE_TIME = 1784736900.0
CHARTER_FIRST_ELIGIBLE_CLOSE_TIME = 1784737800.0

# The executable design was finalized just after the charter's first
# candidate decision timestamp, so that entire close is conservatively
# excluded.  No row captured before the executable freeze receives credit.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784737800.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784738700.0

# This means the feature schema may prospectively count complete rows.  It does
# not mean a model may fit or score; those stay separately fail closed.
EXECUTABLE_DESIGN_FROZEN = True
RUNTIME_SCORING_CONNECTED = False
NOTIFICATION_ELIGIBLE = False
REAL_TRADING_ALLOWED = False
