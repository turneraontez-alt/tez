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
BLEND_HD = REPO / "assets" / "picklo" / "picklo_hd.blend"
BUILDER_HD = REPO / "assets" / "picklo" / "build_picklo_hd.py"


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_picklo_r15_blend_contract():
    """15 uniquely colored mesh parts, R15 names, LowerTorso root."""
    assert BLEND.is_file(), f"missing asset: {BLEND}"
    _load(BUILDER).verify(str(BLEND))  # asserts internally


def test_picklo_hd_blend_contract():
    """High-detail variant: 15 R15 parts, hierarchy, registered materials."""
    assert BLEND_HD.is_file(), f"missing asset: {BLEND_HD}"
    _load(BUILDER_HD).verify(str(BLEND_HD))  # asserts internally
