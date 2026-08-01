"""Dependency-free immutable identity for the frozen RTI V11 protocol."""

DESIGN_ID = "q15-rti-market-residual-cross-asset-regime-v11"
DESIGN_SHA256 = "e4a5d65485d7559e2eaa84a82d1aeca63f1f87a42914916438af9034c79b0480"
EVALUATION_PROTOCOL_ID = "q15-rti-v11-expanding-walk-forward-evaluation-v2"
EVALUATION_PROTOCOL_SHA256 = (
    "04600797bfbb2170c36972a32c40a4acccba52df37804f79a45b454603c1408b"
)
REPORTING_PROTOCOL_ID = "q15-rti-v11-fixed-subgroup-reporting-v1"
REPORTING_PROTOCOL_SHA256 = (
    "e4381605acf7039436813ea8feba78df3a3ccf15efa9c84b57a678bae3d98143"
)
CALIBRATION_REPORTING_PROTOCOL_ID = (
    "q15-rti-v11-fixed-calibration-reporting-v1"
)
CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    "d10553be7b14c761934bfec82ccd5d87c7e859a4080828d2600deda4c691f27c"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_ID = (
    "q15-rti-v11-fixed-selective-value-curve-v1"
)
SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    "7f50aa65edfc96ea5181a00114e0c0efb9a26e8eed41d55a1df2600e66b6ad35"
)

# Prospective promotion uncertainty must preserve the same-close dependence
# structure used by the frozen historical evaluation.  These constants are
# deliberately immutable and shared by the ledger and runtime gate so a
# scorecard cannot silently weaken the audit protocol.
PROSPECTIVE_BOOTSTRAP_VERSION = "q15-rti-paired-close-window-bootstrap-v1"
PROSPECTIVE_BOOTSTRAP_CLUSTER_KEY = "close_time"
PROSPECTIVE_BOOTSTRAP_RESAMPLES = 5000
PROSPECTIVE_BOOTSTRAP_CONFIDENCE_LEVEL = 0.90
PROSPECTIVE_BOOTSTRAP_RANDOM_SEED = 2026072201
PROSPECTIVE_MIN_MEAN_BRIER_IMPROVEMENT = 0.001
PROSPECTIVE_MIN_MEAN_LOG_LOSS_IMPROVEMENT = 0.001
