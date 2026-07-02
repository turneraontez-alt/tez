"""Frozen route-table contract for the Stage 2 app.py split.

The 59 route functions moved from app.py into routes/{api_core,api_v95_books,
api_legacy}.py. This test pins the FULL url map — every rule, method set, and
endpoint name — to the pre-split baseline captured in tests/data_route_table.json.
Any route added, dropped, or renamed (Blueprints would rename endpoints!) fails
here and must update the baseline deliberately in the same commit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

BASELINE = Path(__file__).parent / "data_route_table.json"


def test_route_table_matches_frozen_baseline(monkeypatch):
    monkeypatch.setenv("Q15_AUTOSTART_REFRESH", "0")
    import app as appmod

    rules = sorted(
        (r.rule, sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")), r.endpoint)
        for r in appmod.app.url_map.iter_rules()
    )
    baseline = [tuple(x) if not isinstance(x, tuple) else x for x in
                [(b[0], b[1], b[2]) for b in json.loads(BASELINE.read_text())]]
    assert [list(r) for r in rules] == [list(b) for b in baseline], (
        "url_map drifted from tests/data_route_table.json — if intentional, "
        "regenerate the baseline in the same commit"
    )
