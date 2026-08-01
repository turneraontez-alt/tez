"""Dormant fail-closed runtime bridge for future locked V12 paper artifacts.

This module only binds the shared validated artifact scorer to the immutable
V12 identity.  It has no notification, order, artifact-creation, activation,
or automatic-promotion surface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import rti_microstructure_v12 as v12
from . import rti_microstructure_runtime as shared


COHORTS = shared.COHORTS
MODEL_FAMILY = shared.MODEL_FAMILY
EXPECTED_TEST_STATE_VERSION = shared.EXPECTED_TEST_STATE_VERSION
DEFAULT_ARTIFACT_PATHS = shared.V12_DEFAULT_ARTIFACT_PATHS
ARTIFACT_ENV = shared.V12_ARTIFACT_ENV
EXPECTED_TRAINING_CONFIG = shared.EXPECTED_TRAINING_CONFIG
EXPECTED_ENTRY_POLICY = shared.EXPECTED_ENTRY_POLICY
EXPECTED_WINDOW_WEIGHTING = shared.EXPECTED_WINDOW_WEIGHTING
artifact_fingerprint = shared.artifact_fingerprint
reset_artifact_cache = shared.reset_artifact_cache


def artifact_path(cohort: str) -> Path:
    return shared.artifact_path(cohort, feature_runtime=v12)


def validate_artifact(
    artifact: Mapping[str, Any], expected_cohort: str,
) -> None:
    shared.validate_artifact(
        artifact, expected_cohort, feature_runtime=v12
    )


def load_artifact(
    cohort: str, path: str | Path | None = None,
) -> Mapping[str, Any]:
    return shared.load_artifact(
        cohort, path, feature_runtime=v12
    )


def runtime_prediction(
    row: Mapping[str, Any], path: str | Path | None = None,
) -> dict[str, Any]:
    return shared.runtime_prediction(
        row, path, feature_runtime=v12
    )


def artifact_health(
    cohort: str, path: str | Path | None = None,
) -> dict[str, Any]:
    return shared.artifact_health(
        cohort, path, feature_runtime=v12
    )
