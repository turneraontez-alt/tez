"""Asset contract for assets/picklo/picklo_r15.blend.

Skips wherever the `bpy` module isn't installed (it is NOT a runtime
dependency of the monitor — the .blend is a standalone art asset built by
assets/picklo/build_picklo_r15.py).
"""

import importlib.util
import pathlib

import pytest

pytest.importorskip("bpy")

REPO = pathlib.Path(__file__).resolve().parents[1]
BLEND = REPO / "assets" / "picklo" / "picklo_r15.blend"
BUILDER = REPO / "assets" / "picklo" / "build_picklo_r15.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_picklo_r15", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_picklo_r15_blend_contract():
    """15 uniquely colored mesh parts, R15 names, LowerTorso root."""
    assert BLEND.is_file(), f"missing asset: {BLEND}"
    builder = _load_builder()
    builder.verify(str(BLEND))  # asserts internally
