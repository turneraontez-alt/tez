from __future__ import annotations

import json

from tools import q15_rti_v20_pretest_command as command
from test_q15_rti_v20_modeling import _sealed_population


def test_command_projects_exact_parent_contracts_without_outcomes(tmp_path):
    payload, _labels = _sealed_population()
    path = tmp_path / "feature-seal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = command.load_feature_seal(path)
    rows = command.expected_database_rows(loaded)
    assert len(rows) == 1050
    assert len({row["id"] for row in rows}) == 1050
    assert all(set(row) == {"id", "ticker", "asset", "close_time"} for row in rows)
    assert {row["id"] for row in rows} == {
        row["parent_id"] for row in payload["rows"]
    }


def test_command_projection_contains_no_label_or_resolution_field():
    payload, _labels = _sealed_population()
    forbidden = {
        "official_result", "correct", "resolved_at", "label_survives",
        "result_yes", "settlement_result",
    }
    assert all(
        forbidden.isdisjoint(row)
        for row in command.expected_database_rows(payload)
    )

