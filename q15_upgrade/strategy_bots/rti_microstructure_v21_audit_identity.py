"""Immutable identities for V21's manual label-gated audit."""

from . import rti_microstructure_v21_identity as identity


EVALUATOR_CONTRACT_ID = identity.EVALUATOR_CONTRACT_ID
EVALUATOR_CONTRACT_RELATIVE_PATH = identity.EVALUATOR_CONTRACT_RELATIVE_PATH
EVALUATOR_CONTRACT_SHA256 = identity.EVALUATOR_CONTRACT_SHA256
MODELING_VERSION = "q15-rti-v21-disjoint-pretest-modeling-v3"
PRETEST_RUNNER_VERSION = "q15-rti-v21-one-shot-train-calibration-policy-v2"
PRETEST_STATE_VERSION = "q15-rti-v21-train-calibration-policy-state-v2"
UNTOUCHED_TEST_RUNNER_VERSION = "q15-rti-v21-one-shot-untouched-test-v2"
UNTOUCHED_TEST_STATE_VERSION = "q15-rti-v21-untouched-test-state-v2"
PRETEST_CONFIRMATION = "OPEN_V21_TRAIN_CAL_POLICY_LABELS_ONCE"
UNTOUCHED_TEST_CONFIRMATION = "SCORE_V21_UNTOUCHED_TEST_ONCE"
