"""PickloHD Head — hero part.

Builds the "Head" part (green skull + ears + antennae, Skin/SkinDark two-tone)
plus "LeftEye"/"RightEye" children, per spec.py landmarks. World coords,
character faces -Y, character LEFT is +X.

Technique: lofted horizontal ring cross-sections for the skull (per-ring
half-ellipses with a front "pinch" so the face is flatter than the cranium),
then anatomical displacement fields (smooth-falloff nudges) for brow ridge,
eye sockets, nose, cheekbones, cheek hollows, chin, jaw and the mouth crease.
Ears and antennae are separate lofted shells merged into the same mesh.
"""

import math
import os
import sys
import importlib.util

import bmesh
from mathutils import Vector


# --------------------------------------------------------------------------
# spec loading (shared landmarks / helpers)
# --------------------------------------------------------------------------

def _load_spec():
    name = "picklo_hd_spec"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec.py")
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s)
    sys.modules[name] = m
    s.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# small math helpers
# --------------------------------------------------------------------------

def _smooth01(t):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _fall(d, r):
    """Smooth 1->0 falloff for distance d over radius r."""
    if d >= r:
        return 0.0
    return _smooth01(1.0 - d / r)


def _lerp(a, b, t):
    return a + (b - a) * t


def _catmull_rom(pts, samples):
    """Catmull-Rom sample of a list of Vectors."""
    p = [pts[0]] + list(pts) + [pts[-1]]
    out = []
    nseg = len(pts) - 1
    for k in range(samples):
        s = (k / (samples - 1)) * nseg
        i = min(int(s), nseg - 1)
        t = s - i
        p0, p1, p2, p3 = p[i], p[i + 1], p[i + 2], p[i + 3]
        out.append(0.5 * ((2.0 * p1) + (-p0 + p2) * t
                          + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t * t
                          + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t * t * t))
    return out


# --------------------------------------------------------------------------
# skull loft
# --------------------------------------------------------------------------

_NSEG = 64

# (z, cy, rx, ry_front, ry_back, front_pinch)
_RINGS = [
    (1.585, 0.020, 0.047, 0.050, 0.052, 0.00),   # neck bottom (blends to collar)
    (1.610, 0.020, 0.048, 0.050, 0.054, 0.00),
    (1.638, 0.018, 0.050, 0.053, 0.058, 0.00),   # neck top
    (1.658, 0.004, 0.044, 0.066, 0.066, 0.34),   # jaw underside / chin
    (1.672, 0.000, 0.052, 0.074, 0.072, 0.30),
    (1.684, -0.002, 0.056, 0.079, 0.076, 0.28),  # lower lip
    (1.691, -0.002, 0.058, 0.081, 0.078, 0.26),  # mouth crease
    (1.699, -0.002, 0.060, 0.082, 0.080, 0.25),  # upper lip
    (1.708, -0.001, 0.062, 0.083, 0.082, 0.22),
    (1.718, 0.000, 0.065, 0.084, 0.085, 0.20),   # nose base
    (1.727, 0.001, 0.068, 0.084, 0.088, 0.18),   # nostrils
    (1.736, 0.002, 0.071, 0.084, 0.090, 0.16),   # nose tip level
    (1.748, 0.003, 0.074, 0.085, 0.092, 0.14),
    (1.760, 0.004, 0.076, 0.085, 0.094, 0.13),   # cheekbone
    (1.772, 0.005, 0.078, 0.085, 0.096, 0.12),
    (1.782, 0.005, 0.079, 0.086, 0.097, 0.12),   # eye centre
    (1.792, 0.006, 0.080, 0.088, 0.098, 0.11),
    (1.801, 0.006, 0.080, 0.089, 0.099, 0.10),   # brow
    (1.812, 0.007, 0.081, 0.087, 0.099, 0.09),
    (1.826, 0.008, 0.081, 0.084, 0.099, 0.07),   # forehead
    (1.843, 0.009, 0.080, 0.080, 0.097, 0.05),
    (1.862, 0.011, 0.077, 0.073, 0.093, 0.03),
    (1.880, 0.013, 0.071, 0.063, 0.086, 0.00),
    (1.896, 0.016, 0.061, 0.050, 0.073, 0.00),
    (1.908, 0.019, 0.047, 0.036, 0.055, 0.00),
    (1.918, 0.021, 0.028, 0.021, 0.033, 0.00),   # crown (ngon cap above)
]


def _build_skull(bm):
    rings = []
    for (z, cy, rx, ryf, ryb, pinch) in _RINGS:
        row = []
        for i in range(_NSEG):
            th = 2.0 * math.pi * i / _NSEG
            c, s = math.cos(th), math.sin(th)     # c=+1 at the front (-Y)
            f = _smooth01((c + 0.45) / 0.9)       # blend back->front depth
            ry = ryb + (ryf - ryb) * f
            x = rx * s * (1.0 - pinch * max(0.0, c) ** 2)
            y = cy - ry * c
            row.append(bm.verts.new((x, y, z)))
        rings.append(row)
    for a, b in zip(rings, rings[1:]):
        for i in range(_NSEG):
            j = (i + 1) % _NSEG
            bm.faces.new((a[i], a[j], b[j], b[i]))
    bm.faces.new(tuple(reversed(rings[0])))   # bottom cap (hidden in collar)
    bm.faces.new(tuple(rings[-1]))            # crown cap
    return [v for row in rings for v in row]


# --------------------------------------------------------------------------
# facial displacement fields
# --------------------------------------------------------------------------

# (point(+X side), aniso weights, radius, displacement, mirrored?)
# Mirrored fields flip point.x and disp.x for the -X side.
_FIELDS = [
    # heavy scowling brow ridge: forward + down
    ((0.036, -0.083, 1.803), (1.0, 0.55, 1.35), 0.048, (0.000, -0.016, -0.0045), True),
    # inner brow / glabella knot (scowl)
    ((0.016, -0.088, 1.793), (1.0, 0.60, 1.30), 0.030, (0.000, -0.010, -0.0060), True),
    # deep-set eye sockets
    ((0.034, -0.082, 1.776), (1.1, 0.70, 1.50), 0.030, (0.000, +0.013, 0.000), True),
    # gaunt cheekbones
    ((0.062, -0.048, 1.757), (1.0, 0.80, 1.10), 0.036, (+0.006, -0.006, +0.001), True),
    # hollow cheeks below
    ((0.054, -0.058, 1.714), (1.0, 0.80, 1.10), 0.032, (-0.004, +0.007, 0.000), True),
    # pointed chin
    ((0.000, -0.068, 1.660), (1.25, 0.80, 1.00), 0.030, (0.000, -0.009, -0.007), False),
    # jaw corner definition
    ((0.050, +0.012, 1.680), (1.0, 0.70, 1.00), 0.030, (+0.005, 0.000, 0.000), True),
    # slight temple hollow
    ((0.070, -0.045, 1.815), (1.0, 0.80, 1.00), 0.028, (-0.003, +0.002, 0.000), True),
]

_NOSE_TIP_Z = 1.737
_NOSE_TOP_Z = 1.806
_NOSE_BOT_Z = 1.723
_NOSE_AMT = 0.030

_MOUTH_Z0 = 1.6915
_MOUTH_DROOP = 7.0     # z = z0 - droop * x^2 (downturned corners)
_MOUTH_HALF_W = 0.036


def _mouth_crease_z(x):
    return _MOUTH_Z0 - _MOUTH_DROOP * x * x


def _apply_fields(verts):
    for (p, an, r, d, mirror) in _FIELDS:
        sides = (1.0, -1.0) if mirror else (1.0,)
        for s in sides:
            px, py, pz = p[0] * s, p[1], p[2]
            dx, dy, dz = d[0] * s, d[1], d[2]
            for v in verts:
                q = v.co
                dd = math.sqrt(((q.x - px) * an[0]) ** 2
                               + ((q.y - py) * an[1]) ** 2
                               + ((q.z - pz) * an[2]) ** 2)
                w = _fall(dd, r)
                if w > 0.0:
                    v.co.x += dx * w
                    v.co.y += dy * w
                    v.co.z += dz * w


def _apply_nose(verts):
    for v in verts:
        q = v.co
        if q.y > -0.030 or q.z < _NOSE_BOT_Z - 0.004 or q.z > _NOSE_TOP_Z + 0.01:
            continue
        z = q.z
        if z >= _NOSE_TIP_Z:
            t = (z - _NOSE_TIP_Z) / (_NOSE_TOP_Z - _NOSE_TIP_Z)
            amt = _lerp(_NOSE_AMT, 0.005, _smooth01(t))
            width = _lerp(0.011, 0.016, t)
        else:
            t = (_NOSE_TIP_Z - z) / (_NOSE_TIP_Z - _NOSE_BOT_Z)
            amt = _NOSE_AMT * (1.0 - _smooth01(t))
            width = 0.012
        w = _fall(abs(q.x), width)
        if w <= 0.0:
            continue
        v.co.y -= amt * w
        # pointed, slightly drooping tip
        tipw = _fall(abs(z - _NOSE_TIP_Z), 0.010) * w
        v.co.z -= 0.004 * tipw
    # nostril flare bulges
    for s in (1.0, -1.0):
        for v in verts:
            q = v.co
            dd = math.sqrt(((q.x - s * 0.0135) * 1.0) ** 2
                           + ((q.y + 0.088) * 0.35) ** 2
                           + ((q.z - 1.727) * 1.0) ** 2)
            w = _fall(dd, 0.011)
            if w > 0.0:
                v.co.y -= 0.0035 * w
                v.co.x += s * 0.0015 * w


def _apply_mouth(verts):
    for v in verts:
        q = v.co
        if q.y > -0.045 or abs(q.x) > 0.042 or abs(q.z - _MOUTH_Z0) > 0.030:
            continue
        cz = _mouth_crease_z(q.x)
        dz = q.z - cz
        wx = _fall(abs(q.x), _MOUTH_HALF_W)
        v.co.y += 0.0050 * _fall(abs(dz), 0.006) * wx          # the crease
        v.co.y -= 0.0022 * _fall(abs(dz - 0.0085), 0.006) * wx  # upper lip roll
        v.co.y -= 0.0016 * _fall(abs(dz + 0.0085), 0.006) * wx  # lower lip roll


# --------------------------------------------------------------------------
# ears — long pointed blades sweeping up-and-back, tips near crown height
# --------------------------------------------------------------------------

def _add_ear(bm, side):
    base = Vector((side * 0.050, 0.012, 1.756))
    tip = Vector((side * 0.106, 0.066, 1.902))
    mid = (base + tip) * 0.5 + Vector((side * 0.014, 0.008, -0.006))

    n_rings = 7
    widths = [0.042, 0.036, 0.029, 0.021, 0.013, 0.006, 0.0018]
    thick = [0.013, 0.011, 0.009, 0.0065, 0.0042, 0.0026, 0.0010]
    nseg = 16

    u0 = Vector((side * 0.92, -0.34, 0.16)).normalized()  # outward, tilted fwd
    rings = []
    for k in range(n_rings):
        t = k / (n_rings - 1)
        c0 = (1 - t) ** 2 * base + 2 * (1 - t) * t * mid + t * t * tip
        tan = (2 * (1 - t) * (mid - base) + 2 * t * (tip - mid)).normalized()
        vdir = tan.cross(u0).normalized()
        u = vdir.cross(tan).normalized()
        scoop_amp = 0.0050 * _fall(abs(t - 0.28), 0.34)  # inner-ear concavity
        row = []
        for i in range(nseg):
            a = 2.0 * math.pi * i / nseg
            ca, sa = math.cos(a), math.sin(a)
            co = c0 + u * (thick[k] * ca) + vdir * (widths[k] * sa)
            if ca > 0.0:  # outer (forward-tilted) face gets the concave dish
                co -= u * (scoop_amp * ca * (1.0 - abs(sa)) ** 0.8)
            row.append(bm.verts.new(co))
        rings.append(row)
    for a_row, b_row in zip(rings, rings[1:]):
        for i in range(nseg):
            j = (i + 1) % nseg
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    bm.faces.new(tuple(reversed(rings[0])))   # buried in the skull
    bm.faces.new(tuple(rings[-1]))


# --------------------------------------------------------------------------
# antennae — thin segmented, rising from the forehead, arcing forward then
# drooping; slight bulge per segment, tapered tips
# --------------------------------------------------------------------------

def _add_antenna(bm, side):
    ctrl = [Vector((side * x, y, z)) for (x, y, z) in (
        (0.018, -0.052, 1.842),   # buried in the forehead
        (0.022, -0.075, 1.862),
        (0.027, -0.094, 1.876),
        (0.033, -0.112, 1.881),
        (0.039, -0.128, 1.876),
        (0.044, -0.141, 1.862),
        (0.048, -0.150, 1.845),
        (0.051, -0.156, 1.826),
        (0.053, -0.159, 1.808),
    )]
    n_rings = 13
    nseg = 10
    path = _catmull_rom(ctrl, n_rings)

    # parallel-transported frames to avoid twist
    rings = []
    prev_u = None
    for k in range(n_rings):
        t = k / (n_rings - 1)
        if k == 0:
            tan = (path[1] - path[0]).normalized()
        elif k == n_rings - 1:
            tan = (path[-1] - path[-2]).normalized()
        else:
            tan = (path[k + 1] - path[k - 1]).normalized()
        if prev_u is None:
            ref = Vector((1.0, 0.0, 0.0))
            v = tan.cross(ref)
            if v.length < 1e-6:
                v = tan.cross(Vector((0.0, 0.0, 1.0)))
            v.normalize()
            u = v.cross(tan).normalized()
        else:
            u = (prev_u - tan * prev_u.dot(tan)).normalized()
            v = tan.cross(u).normalized()
        prev_u = u
        base_r = _lerp(0.0062, 0.0016, t ** 0.9)
        r = base_r * (1.0 + 0.18 * math.cos(2.0 * math.pi * 4.5 * t))
        row = []
        for i in range(nseg):
            a = 2.0 * math.pi * i / nseg
            co = path[k] + u * (r * math.cos(a)) + v * (r * math.sin(a))
            row.append(bm.verts.new(co))
        rings.append(row)
    for a_row, b_row in zip(rings, rings[1:]):
        for i in range(nseg):
            j = (i + 1) % nseg
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    bm.faces.new(tuple(reversed(rings[0])))
    bm.faces.new(tuple(rings[-1]))


# --------------------------------------------------------------------------
# SkinDark accent regions (material_index 1)
# --------------------------------------------------------------------------

def _assign_dark(bm):
    for f in bm.faces:
        fc = f.calc_center_median()
        ax = abs(fc.x)
        # brow underside shadow accent
        if (fc.y < -0.060 and 1.7785 < fc.z < 1.799 and 0.0075 < ax < 0.0585
                and f.normal.z < 0.20):
            f.material_index = 1
            continue
        # mouth crease line (downturned)
        if ax < 0.033 and fc.y < -0.055:
            if abs(fc.z - _mouth_crease_z(fc.x)) < 0.0042:
                f.material_index = 1
                continue
        # nostril accents
        for s in (1.0, -1.0):
            dd = math.sqrt(((fc.x - s * 0.0135) * 1.0) ** 2
                           + ((fc.y + 0.090) * 0.5) ** 2
                           + ((fc.z - 1.7265) * 1.0) ** 2)
            if dd < 0.006:
                f.material_index = 1
                break


# --------------------------------------------------------------------------
# eyes
# --------------------------------------------------------------------------

_EYE_R = 0.0165
_EYE_CENTER = (0.032, -0.053, 1.781)


def _build_eye(bpy, col, spec, name, side):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=22, v_segments=16, radius=_EYE_R)
    center = Vector((side * _EYE_CENTER[0], _EYE_CENTER[1], _EYE_CENTER[2]))
    bmesh.ops.translate(bm, verts=bm.verts, vec=center)
    # Stern gaze: both eyes look straight forward, iris angled very slightly
    # inward so the small dark iris reads as a focused stare, not wall-eyed.
    fwd = Vector((-side * 0.10, -1.0, -0.04)).normalized()
    cos_iris = math.cos(math.radians(26.0))
    for f in bm.faces:
        d = (f.calc_center_median() - center).normalized()
        if d.dot(fwd) > cos_iris:
            f.material_index = 1
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(
        bpy, col, name, bm, tuple(center),
        materials=("PickloHD_EyeWhite", "PickloHD_Pupil"), subsurf=2)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build(bpy, col):
    spec = _load_spec()

    bm = bmesh.new()
    skull_verts = _build_skull(bm)
    _apply_nose(skull_verts)
    _apply_fields(skull_verts)
    _apply_mouth(skull_verts)
    for s in (1.0, -1.0):
        _add_ear(bm, s)
        _add_antenna(bm, s)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    _assign_dark(bm)

    head = spec.object_from_bm(
        bpy, col, "Head", bm, spec.PARTS["Head"][0],
        materials=("PickloHD_Skin", "PickloHD_SkinDark"), subsurf=2)

    leye = _build_eye(bpy, col, spec, "LeftEye", +1.0)
    reye = _build_eye(bpy, col, spec, "RightEye", -1.0)

    bpy.context.view_layer.update()
    for eye in (leye, reye):
        eye.parent = head
        eye.matrix_parent_inverse = head.matrix_world.inverted()

    return [head, leye, reye]
