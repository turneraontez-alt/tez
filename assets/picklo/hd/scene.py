"""Studio lighting, backdrop and cameras for the HD Picklo model.

setup(bpy, scene) turns the current scene into a full-body character studio:
a seamless cove backdrop (floor sweeping into a curved back wall), soft
three-point lighting, and two cameras (Camera_Full full body 3/4, Camera_Head
head-and-shoulders). Assumes the model occupies roughly x +-0.45, y +-0.35,
z 0..2.05 and faces -Y.
"""

import math
import os
import sys

import bmesh
from mathutils import Vector

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import spec  # noqa: E402


def _backdrop(bpy, scene):
    """Curved cove: floor (+Y behind) sweeping up into a back wall, one mesh."""
    bm = bmesh.new()
    width = 6.0
    floor_front = -3.0   # toward camera (-Y)
    cove_start = 1.2     # +Y where floor starts curving up
    radius = 1.6
    nrail = 24
    # profile in (y, z): flat floor, quarter-circle cove, vertical wall
    prof = []
    ny = 8
    for k in range(ny):
        y = floor_front + (cove_start - floor_front) * (k / (ny - 1))
        prof.append((y, 0.0))
    nc = 12
    for k in range(1, nc + 1):
        a = (math.pi / 2.0) * (k / nc)
        y = cove_start + radius * math.sin(a)
        z = radius * (1.0 - math.cos(a))
        prof.append((y, z))
    nw = 6
    for k in range(1, nw + 1):
        prof.append((cove_start + radius, radius + 2.0 * (k / nw)))
    rows = []
    for (y, z) in prof:
        row = []
        for i in range(nrail):
            x = -width / 2 + width * i / (nrail - 1)
            row.append(bm.verts.new((x, y, z)))
        rows.append(row)
    for a_row, b_row in zip(rows, rows[1:]):
        for i in range(nrail - 1):
            bm.faces.new((a_row[i], a_row[i + 1], b_row[i + 1], b_row[i]))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("Backdrop")
    bm.to_mesh(mesh)
    bm.free()
    for p in mesh.polygons:
        p.use_smooth = True
    obj = bpy.data.objects.new("Backdrop", mesh)
    mat = bpy.data.materials.get("Backdrop_Mat") or \
        bpy.data.materials.new("Backdrop_Mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    col = (*[spec.srgb_to_linear(c) for c in (0.55, 0.55, 0.57)], 1.0)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = col
        bsdf.inputs["Roughness"].default_value = 0.9
    mat.diffuse_color = col
    obj.data.materials.append(mat)
    scene.collection.objects.link(obj)
    return obj


def _world(bpy, scene):
    world = bpy.data.worlds.get("PickloWorld") or \
        bpy.data.worlds.new("PickloWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.30, 0.32, 0.36, 1.0)
        bg.inputs["Strength"].default_value = 0.35
    scene.world = world


def _area(bpy, scene, name, loc, target, energy, size, color=(1, 1, 1)):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    data.color = color
    ob = bpy.data.objects.new(name, data)
    ob.location = Vector(loc)
    direction = Vector(target) - ob.location
    ob.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(ob)
    return ob


def _lights(bpy, scene):
    body = Vector((0.0, 0.0, 1.15))
    head = Vector((0.0, -0.05, 1.75))
    # Energies tuned for AgX tone-mapping at ~3-4 m throw (no highlight clip).
    # Key: large soft, front-left-high, warm
    _area(bpy, scene, "Key", (-2.6, -3.2, 3.1), head, 320, 2.4,
          color=(1.0, 0.96, 0.9))
    # Fill: softer, front-right-low, cool
    _area(bpy, scene, "Fill", (2.8, -2.4, 0.9), body, 110, 3.0,
          color=(0.85, 0.9, 1.0))
    # Rim / kicker: behind-above, pops the silhouette off the backdrop
    _area(bpy, scene, "Rim", (1.2, 3.2, 3.4), Vector((0, 0, 1.4)), 420, 1.6,
          color=(1.0, 0.98, 0.95))
    # subtle antenna/shoulder kick from high behind-left
    _area(bpy, scene, "RimL", (-1.8, 2.4, 3.2), head, 180, 1.0)


def _camera(bpy, scene, name, loc, target, lens, sensor=36.0):
    cam_data = bpy.data.cameras.new(name)
    cam_data.lens = lens
    cam_data.sensor_width = sensor
    cam = bpy.data.objects.new(name, cam_data)
    cam.location = Vector(loc)
    direction = Vector(target) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(cam)
    return cam


def setup(bpy, scene):
    _world(bpy, scene)
    _backdrop(bpy, scene)
    _lights(bpy, scene)

    # Full-body 3/4 hero from slightly low
    full = _camera(bpy, scene, "Camera_Full",
                   loc=(1.7, -3.6, 1.05), target=(0.0, 0.0, 1.02),
                   lens=68.0)
    # Head-and-shoulders close-up 3/4 from slightly below
    _camera(bpy, scene, "Camera_Head",
            loc=(0.9, -1.7, 1.60), target=(0.0, -0.02, 1.76),
            lens=85.0)

    scene.camera = full
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.cycles.device = "CPU"
    try:
        scene.cycles.use_denoising = True
    except Exception:
        pass
    scene.render.resolution_x = 1150
    scene.render.resolution_y = 1500
    # AgX gives filmic highlight rolloff (no clipping); Punchy keeps saturation.
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Punchy"
    except (TypeError, AttributeError):
        scene.view_settings.view_transform = "Filmic"
    return scene
