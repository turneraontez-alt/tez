#!/usr/bin/env python3
"""Build the "Picklo" R15 character as a Blender file.

Generates an R15-style blocky rig (the 15 Roblox body parts) where every part
is its own mesh object with its own uniquely colored material, so each piece
can be selected, re-colored, or animated independently.

Design choices:
- Palette matches Piccolo (Dragon Ball): green skin, purple gi, blue sash,
  pink ribbed arm sections, orange shoes. Symmetric parts get slightly
  different shades so all 15 materials stay numerically unique and every part
  reads as its own piece.
- The Head mesh carries Piccolo's two forward-tilted antennae and pointed
  side ears (same object/material, so the model stays exactly 15 parts).
- Each object's origin sits at its joint (hip, knee, elbow, ...) and parts are
  parented LowerTorso -> UpperTorso -> Head/arms, LowerTorso -> legs, so
  rotating any part pivots naturally at the joint.
- Every box is shrunk by a hair (0.02 studs) so adjacent parts never share a
  coplanar face (avoids viewport z-fighting) and the separation is visible.

Usage:
    python3 assets/picklo/build_picklo_r15.py [--out FILE.blend]
        [--render FILE.png] [--verify FILE.blend]

Requires the `bpy` module (pip install bpy) or running inside Blender:
    blender --background --python assets/picklo/build_picklo_r15.py
"""

from __future__ import annotations

import argparse
import math
import sys

# (name, size (x,y,z), world center, pivot/origin (joint), parent, RGB color)
# Coordinates in "studs": character faces -Y, +X is the character's left.
GAP = 0.02  # shrink per axis so touching parts never z-fight

R15_PARTS = [
    # name              size                 center               pivot (joint)       parent          color (sRGB)
    ("LowerTorso",      (2.00, 1.00, 0.45), (0.0, 0.0, 2.225),  (0.0, 0.0, 2.00),  None,           (0.16, 0.38, 0.78)),  # blue sash
    ("UpperTorso",      (2.00, 1.00, 1.55), (0.0, 0.0, 3.225),  (0.0, 0.0, 2.45),  "LowerTorso",   (0.44, 0.26, 0.64)),  # purple gi
    ("Head",            (1.20, 1.20, 1.20), (0.0, 0.0, 4.600),  (0.0, 0.0, 4.00),  "UpperTorso",   (0.45, 0.78, 0.25)),  # green skin
    # Arms: pink ribbed sections, green hands (character's left = +X)
    ("LeftUpperArm",    (1.00, 1.00, 0.85), (1.5, 0.0, 3.575),  (1.5, 0.0, 4.00),  "UpperTorso",   (0.94, 0.60, 0.66)),
    ("LeftLowerArm",    (1.00, 1.00, 0.80), (1.5, 0.0, 2.750),  (1.5, 0.0, 3.15),  "LeftUpperArm", (0.98, 0.72, 0.76)),
    ("LeftHand",        (0.90, 0.90, 0.35), (1.5, 0.0, 2.175),  (1.5, 0.0, 2.35),  "LeftLowerArm", (0.36, 0.68, 0.20)),
    ("RightUpperArm",   (1.00, 1.00, 0.85), (-1.5, 0.0, 3.575), (-1.5, 0.0, 4.00), "UpperTorso",   (0.90, 0.56, 0.62)),
    ("RightLowerArm",   (1.00, 1.00, 0.80), (-1.5, 0.0, 2.750), (-1.5, 0.0, 3.15), "RightUpperArm", (0.94, 0.68, 0.72)),
    ("RightHand",       (0.90, 0.90, 0.35), (-1.5, 0.0, 2.175), (-1.5, 0.0, 2.35), "RightLowerArm", (0.32, 0.62, 0.18)),
    # Legs: purple gi pants, orange shoes
    ("LeftUpperLeg",    (1.00, 1.00, 0.85), (0.5, 0.0, 1.575),  (0.5, 0.0, 2.00),  "LowerTorso",   (0.38, 0.21, 0.56)),
    ("LeftLowerLeg",    (1.00, 1.00, 0.80), (0.5, 0.0, 0.750),  (0.5, 0.0, 1.15),  "LeftUpperLeg", (0.31, 0.17, 0.47)),
    ("LeftFoot",        (1.00, 1.30, 0.35), (0.5, -0.15, 0.175), (0.5, 0.0, 0.35), "LeftLowerLeg", (0.92, 0.48, 0.10)),
    ("RightUpperLeg",   (1.00, 1.00, 0.85), (-0.5, 0.0, 1.575), (-0.5, 0.0, 2.00), "LowerTorso",   (0.35, 0.19, 0.52)),
    ("RightLowerLeg",   (1.00, 1.00, 0.80), (-0.5, 0.0, 0.750), (-0.5, 0.0, 1.15), "RightUpperLeg", (0.28, 0.15, 0.43)),
    ("RightFoot",       (1.00, 1.30, 0.35), (-0.5, -0.15, 0.175), (-0.5, 0.0, 0.35), "RightLowerLeg", (0.87, 0.44, 0.09)),
]

MODEL_COLLECTION = "Picklo_R15"
ENV_COLLECTION = "Environment"


def _add_cube(bm, scale, offset, rot=None, shape=None):
    """Add a unit cube to `bm`, optionally reshape it in unit space, then
    scale / rotate (Euler radians) / translate only the new geometry."""
    import bmesh
    from mathutils import Euler, Matrix, Vector

    verts = bmesh.ops.create_cube(bm, size=1.0)["verts"]
    if shape:
        for v in verts:
            shape(v.co)
    mat = Matrix.Translation(Vector(offset))
    if rot:
        mat = mat @ Euler(rot, "XYZ").to_matrix().to_4x4()
    mat = mat @ Matrix.Diagonal((*scale, 1.0))
    bmesh.ops.transform(bm, matrix=mat, verts=verts)
    return verts


def _finish_mesh(bpy, bm, name):
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return mesh


def _box_mesh(bpy, name, size, center, pivot, shape=None):
    """Cuboid mesh (outward normals) with vertices relative to the pivot."""
    import bmesh

    scale = [max(s - GAP, 0.01) for s in size]
    offset = [c - p for c, p in zip(center, pivot)]
    bm = bmesh.new()
    _add_cube(bm, scale, offset, shape=shape)
    return _finish_mesh(bpy, bm, name)


def _head_mesh(bpy, name, size, center, pivot):
    """Piccolo head: cube + two forward-tilted antennae + pointed side ears.

    All in one mesh so the model stays exactly 15 objects. Coordinates are
    head-local (relative to the neck pivot); the head cube spans z 0..1.2.
    """
    import bmesh
    import math

    bm = bmesh.new()
    scale = [max(s - GAP, 0.01) for s in size]
    offset = [c - p for c, p in zip(center, pivot)]
    _add_cube(bm, scale, offset)

    def spike(co):  # collapse the top face toward a point
        if co.z > 0:
            co.x *= 0.15
            co.y *= 0.15

    tilt = math.radians(40)  # antennae lean forward (character faces -Y)
    for sx in (1.0, -1.0):
        _add_cube(bm, (0.11, 0.11, 0.55), (sx * 0.17, -0.30, 1.32),
                  rot=(tilt, 0.0, 0.0), shape=spike)
        # ears: tapered wedges on the head sides, tips up and slightly out
        _add_cube(bm, (0.30, 0.36, 0.72), (sx * 0.74, 0.0, 0.74),
                  rot=(0.0, sx * math.radians(18), 0.0), shape=spike)

    return _finish_mesh(bpy, bm, name)


def _toe_taper(co):
    """Slope the front-top of the shoe so it reads as Piccolo's pointed toe."""
    if co.y < 0 and co.z > 0:
        co.z = 0.0
        co.x *= 0.8


def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _material(bpy, name, rgb):
    """Material from an sRGB color (Blender color slots expect linear)."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    rgba = (*[_srgb_to_linear(c) for c in rgb], 1.0)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Roughness"].default_value = 0.55
    mat.diffuse_color = rgba  # solid-shading viewport color
    return mat


def build(out_path, render_path=None):
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Picklo"

    model_col = bpy.data.collections.new(MODEL_COLLECTION)
    env_col = bpy.data.collections.new(ENV_COLLECTION)
    scene.collection.children.link(model_col)
    scene.collection.children.link(env_col)

    objects = {}
    for name, size, center, pivot, parent, color in R15_PARTS:
        if name == "Head":
            mesh = _head_mesh(bpy, name, size, center, pivot)
        else:
            shape = _toe_taper if name.endswith("Foot") else None
            mesh = _box_mesh(bpy, name, size, center, pivot, shape=shape)
        obj = bpy.data.objects.new(name, mesh)
        obj.location = pivot
        obj.data.materials.append(_material(bpy, f"Picklo_{name}", color))
        bevel = obj.modifiers.new("Bevel", "BEVEL")
        bevel.width = 0.02
        bevel.segments = 2
        model_col.objects.link(obj)
        objects[name] = obj

    # Parent after all objects exist; keep world transforms. The view layer
    # must be evaluated first or every matrix_world is still identity and the
    # parent offsets accumulate down the chain (exploded model).
    bpy.context.view_layer.update()
    for name, _size, _center, _pivot, parent, _color in R15_PARTS:
        if parent:
            child = objects[name]
            child.parent = objects[parent]
            child.matrix_parent_inverse = objects[parent].matrix_world.inverted()

    # --- environment: ground, sun, camera (kept out of the model collection) ---
    ground_mesh = bpy.data.meshes.new("Ground")
    g = 40.0
    ground_mesh.from_pydata(
        [(-g, -g, 0), (g, -g, 0), (g, g, 0), (-g, g, 0)], [], [(0, 1, 2, 3)]
    )
    ground_mesh.update()
    ground = bpy.data.objects.new("Ground", ground_mesh)
    ground.data.materials.append(_material(bpy, "Ground", (0.82, 0.82, 0.80)))
    env_col.objects.link(ground)

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 3.5
    sun_data.angle = math.radians(15)
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.location = (4.0, -6.0, 9.0)
    sun.rotation_euler = (math.radians(40), math.radians(-15), math.radians(20))
    env_col.objects.link(sun)

    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = 50.0
    cam = bpy.data.objects.new("Camera", cam_data)
    cam.location = (5.0, -12.0, 4.6)
    from mathutils import Vector

    look = Vector((0.0, 0.0, 2.6)) - cam.location
    cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    env_col.objects.link(cam)
    scene.camera = cam

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.85, 0.88, 0.90, 1.0)
        bg.inputs["Strength"].default_value = 0.8
    scene.world = world
    # AgX washes the flat part colors into pastels; Standard keeps them true.
    scene.view_settings.view_transform = "Standard"

    if render_path:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 48
        scene.cycles.device = "CPU"
        scene.render.resolution_x = 900
        scene.render.resolution_y = 1100
        scene.render.filepath = render_path
        bpy.ops.render.render(write_still=True)

    bpy.ops.wm.save_as_mainfile(filepath=out_path, compress=True)
    print(f"saved: {out_path} ({len(objects)} parts)")


def verify(blend_path):
    """Reload the .blend in-process and assert the 15-part contract."""
    import bpy

    bpy.ops.wm.open_mainfile(filepath=blend_path)
    col = bpy.data.collections.get(MODEL_COLLECTION)
    assert col is not None, f"missing collection {MODEL_COLLECTION}"
    names = sorted(o.name for o in col.objects)
    expected = sorted(p[0] for p in R15_PARTS)
    assert names == expected, f"part mismatch: {names}"
    colors = set()
    for obj in col.objects:
        assert obj.type == "MESH", f"{obj.name} is not a mesh"
        assert len(obj.data.materials) == 1, f"{obj.name} material count"
        mat = obj.data.materials[0]
        assert mat.name == f"Picklo_{obj.name}", f"{obj.name} material name"
        colors.add(tuple(round(c, 4) for c in mat.diffuse_color[:3]))
    assert len(colors) == 15, f"expected 15 unique colors, got {len(colors)}"
    roots = [o for o in col.objects if o.parent is None]
    assert [o.name for o in roots] == ["LowerTorso"], f"bad root: {roots}"
    head = col.objects["Head"]
    assert len(head.data.polygons) >= 18, "head missing antennae/ears"
    print(f"verify OK: 15 uniquely colored parts, root=LowerTorso, in {blend_path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="assets/picklo/picklo_r15.blend")
    ap.add_argument("--render", default=None, help="also render a preview PNG")
    ap.add_argument("--verify", default=None, metavar="BLEND",
                    help="verify an existing .blend instead of building")
    args = ap.parse_args(argv)
    if args.verify:
        verify(args.verify)
    else:
        build(args.out, render_path=args.render)


if __name__ == "__main__":
    # Blender invokes scripts with its own argv; take args after a lone "--".
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None
    main(argv)
