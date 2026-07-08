#!/usr/bin/env python3
"""Assemble the high-detail Picklo (Piccolo, Dragon Ball) model.

Loads the component modules in assets/picklo/hd/ (spec, materials, torso,
head, arms, hands, legs, scene), builds all 15 R15 parts with joint-pivot
origins, wires the LowerTorso-rooted hierarchy, lights the studio, and saves
the .blend. Companion to build_picklo_r15.py (the blocky original) — this one
targets a realistic look: subdivided organic meshes, SSS skin, modeled cloth.

Usage:
    python3 assets/picklo/build_picklo_hd.py [--out FILE.blend]
        [--render PREFIX] [--samples N] [--verify FILE.blend]

Requires the `bpy` module (pip install bpy) or Blender's own Python.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys

HD_DIR = pathlib.Path(__file__).resolve().parent / "hd"
BUILD_ORDER = ("torso", "head", "arms", "hands", "legs")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HD_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(out_path, render_prefix=None, samples=128):
    import bpy

    spec = _load("spec")
    scene, col = spec.reset_scene(bpy)
    _load("materials").register(bpy)
    for modname in BUILD_ORDER:
        _load(modname).build(bpy, col)

    bpy.context.view_layer.update()
    objs = {o.name: o for o in col.objects}
    missing = [n for n in spec.PARTS if n not in objs]
    assert not missing, f"builders did not produce: {missing}"
    for name, (_pivot, parent) in spec.PARTS.items():
        if parent and objs[name].parent is None:
            objs[name].parent = objs[parent]
            objs[name].matrix_parent_inverse = objs[parent].matrix_world.inverted()

    _load("scene").setup(bpy, scene)
    scene.cycles.samples = samples

    bpy.ops.wm.save_as_mainfile(filepath=str(out_path), compress=True)
    print(f"saved: {out_path}")

    if render_prefix:
        for cam_name, tag in (("Camera_Full", "full"), ("Camera_Head", "head")):
            cam = bpy.data.objects.get(cam_name)
            if cam is None:
                print(f"WARNING: no camera {cam_name}, skipping {tag} render")
                continue
            scene.camera = cam
            scene.render.filepath = f"{render_prefix}_{tag}.png"
            bpy.ops.render.render(write_still=True)
            print(f"rendered: {scene.render.filepath}")


def verify(blend_path):
    """Reload the .blend and assert the HD asset contract."""
    import bpy

    spec = _load("spec")
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    col = bpy.data.collections.get(spec.MODEL_COLLECTION)
    assert col is not None, f"missing collection {spec.MODEL_COLLECTION}"

    objs = {o.name: o for o in col.objects}
    for name, (_pivot, parent) in spec.PARTS.items():
        obj = objs.get(name)
        assert obj is not None and obj.type == "MESH", f"missing part {name}"
        want = parent
        got = obj.parent.name if obj.parent else None
        assert got == want, f"{name}: parent {got!r} != {want!r}"
        assert len(obj.data.vertices) >= 60, f"{name}: too coarse"
        assert obj.data.materials, f"{name}: no materials"
        for m in obj.data.materials:
            assert m and m.name in spec.MATS, f"{name}: alien material {m}"

    part_names = set(spec.PARTS)
    for obj in col.objects:
        if obj.name in part_names:
            continue
        anc = obj.parent
        while anc is not None and anc.name not in part_names:
            anc = anc.parent
        assert anc is not None, f"extra object {obj.name} not under any part"

    for mname in spec.MATS:
        assert mname in bpy.data.materials, f"material not registered: {mname}"
    total = sum(len(objs[n].data.vertices) for n in spec.PARTS)
    assert total >= 5000, f"model too coarse overall ({total} cage verts)"
    assert bpy.context.scene.camera is not None, "no active camera"
    print(f"verify OK: 15 HD parts ({total} cage verts), hierarchy + materials"
          f" + studio present in {blend_path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="assets/picklo/picklo_hd.blend")
    ap.add_argument("--render", default=None, metavar="PREFIX",
                    help="render Camera_Full/Camera_Head to PREFIX_*.png")
    ap.add_argument("--samples", type=int, default=128)
    ap.add_argument("--verify", default=None, metavar="BLEND",
                    help="verify an existing .blend instead of building")
    args = ap.parse_args(argv)
    if args.verify:
        verify(args.verify)
    else:
        build(args.out, render_prefix=args.render, samples=args.samples)


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None
    main(argv)
