"""Reproducibility & lineage (addendum Section M).

Every prediction/result is stamped with the exact versions and a config hash so
it can be traced to code + configuration. Data-side hashes (dataset_hash,
training/validation/calibration/test periods) are filled by the harness when real
data is present; here we provide the code/config side that is always available.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, fields
from typing import Any

# Bump these deliberately when the corresponding contract changes.
FEATURE_SCHEMA_VERSION = "challenger-features-v1"
LABEL_VERSION = "kalshi-official-result-v1"
SETTLEMENT_DEFINITION_VERSION = "UNKNOWN-pending-human-confirmation"
DECISION_POLICY_VERSION = "challenger-decision-v1"
EXECUTION_COST_VERSION = "challenger-costs-v1"


def code_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def config_hash(config) -> str:
    try:
        data = {f.name: getattr(config, f.name) for f in fields(config)}
    except TypeError:
        data = dict(config)
    blob = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def lineage_record(config, *, calibrator_version: str = "identity",
                   dataset_hash: str | None = None,
                   training_period: Any = None, validation_period: Any = None,
                   calibration_period: Any = None, test_period: Any = None,
                   random_seed: int | None = None) -> dict:
    return {
        "code_commit": code_commit(),
        "config_hash": config_hash(config),
        "model_version": getattr(config, "model_version", "challenger-v1"),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_version": LABEL_VERSION,
        "settlement_definition_version": SETTLEMENT_DEFINITION_VERSION,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "execution_cost_version": EXECUTION_COST_VERSION,
        "calibrator_version": calibrator_version,
        "dataset_hash": dataset_hash or "NOT_PROVIDED",
        "training_period": training_period or "NOT_PROVIDED",
        "validation_period": validation_period or "NOT_PROVIDED",
        "calibration_period": calibration_period or "NOT_PROVIDED",
        "test_period": test_period or "NOT_PROVIDED",
        "random_seed": random_seed,
    }
