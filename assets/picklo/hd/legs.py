"""Legs for the HD Picklo (Piccolo) model.

Builds LeftUpperLeg + LeftLowerLeg + LeftFoot, then mirrors the three for the
Right* twins. Leg axis at x = +-hip_x.

- UpperLeg: baggy purple gi pant, much wider than the leg, big soft vertical
  folds, slight forward thigh mass.
- LowerLeg: pant stays baggy then GATHERS into a cinched cuff above the ankle
  (bunched fold rings), then a narrow ankle.
- Foot: orange pointed slipper with an upturned toe reaching forward (-Y),
  rounded heel, ankle collar, lighter sole underside (material_index).
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

L = spec.L
_HX = L["hip_x"]
_NSEG = 28


def _smooth01(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _g(d, w):
    return math.exp(-(d / w) ** 2)


def _dt(t, t0):
    return math.atan2(math.sin(t - t0), math.cos(t - t0))


def _loft(bm, rings):
    rows = []
    for row in rings:
        rows.append(row)
    for a_row, b_row in zip(rows, rows[1:]):
        for i in range(_NSEG):
            j = (i + 1) % _NSEG
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    return rows


def _cap(bm, ring, up):
    c = Vector((0, 0, 0))
    for v in ring:
        c += v.co
    c /= len(ring)
    c.z += up * 0.006
    vc = bm.verts.new(c)
    n = len(ring)
    for j in range(n):
        if up > 0:
            bm.faces.new((ring[j], ring[(j + 1) % n], vc))
        else:
            bm.faces.new((ring[(j + 1) % n], ring[j], vc))


# baggy-pant vertical folds: (angle, amplitude)
_FOLDS = [(0.0, 0.010), (0.9, 0.014), (-0.9, 0.014),
          (1.9, 0.012), (-1.9, 0.012), (math.pi, 0.010)]


def _build_upper(bpy, col):
    z_hip, z_knee = L["hip"], L["knee"]
    bm = bmesh.new()
    rings = []
    n = 20
    for k in range(n):
        t = k / (n - 1)
        z = z_hip - (z_hip - z_knee) * t
        cx = _HX
        # baggy: wide at thigh, tapering a bit toward the knee
        base = 0.088 - 0.020 * _smooth01(t)
        front = 0.14 * (1.0 - abs(t - 0.3) / 0.7)  # thigh mass forward
        row = []
        for i in range(_NSEG):
            a = -math.pi + 2.0 * math.pi * i / _NSEG
            c, s = math.cos(a), math.sin(a)
            r = base
            for t0, amp in _FOLDS:
                r += amp * _g(_dt(a, t0), 0.28)
            rx = r
            ry = r * (1.0 + max(0.0, front) * max(0.0, c))
            row.append(bm.verts.new((cx + rx * s, -ry * c, z)))
        rings.append(row)
    _loft(bm, rings)
    _cap(bm, rings[0], +1)
    _cap(bm, rings[-1], -1)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "LeftUpperLeg", bm,
                               spec.PARTS["LeftUpperLeg"][0],
                               materials=("PickloHD_Gi",), subsurf=2)


def _build_lower(bpy, col):
    z_knee, z_ank = L["knee"], L["ankle"]
    bm = bmesh.new()
    rings = []
    n = 26
    cinch = 0.32   # fraction from ankle where the cuff gathers
    # ankle stays a believable tube (cloth around the leg), not a pinched point
    ankle_r = 0.040
    for k in range(n):
        t = k / (n - 1)  # 0 at knee, 1 at ankle
        z = z_knee - (z_knee - z_ank) * t
        cx = _HX
        # baggy upper -> gathered cuff -> snug (but not zero) ankle
        if t < 1.0 - cinch:
            base = 0.070 - 0.008 * _smooth01(t / (1.0 - cinch))
        else:
            tt = (t - (1.0 - cinch)) / cinch
            base = 0.062 - (0.062 - ankle_r) * _smooth01(tt)
        # bunched fold rings where the pant gathers above the ankle
        gather = 0.0
        if t > 1.0 - cinch - 0.05:
            zc = (z - z_ank)
            for zr in (0.085, 0.125, 0.165):
                gather += 0.011 * _g(zc - zr, 0.015)
        front = 0.10 * (1.0 - t)  # calf mass, slight
        row = []
        for i in range(_NSEG):
            a = -math.pi + 2.0 * math.pi * i / _NSEG
            c, s = math.cos(a), math.sin(a)
            r = base + gather
            if t < 1.0 - cinch:
                for t0, amp in _FOLDS:
                    r += amp * 0.7 * _g(_dt(a, t0), 0.28)
            rx = r
            ry = r * (1.0 + max(0.0, front) * max(0.0, c))
            row.append(bm.verts.new((cx + rx * s, -ry * c, z)))
        rings.append(row)
    _loft(bm, rings)
    _cap(bm, rings[0], +1)
    _cap(bm, rings[-1], -0.2)  # near-flat ankle cap (no carrot point)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "LeftLowerLeg", bm,
                               spec.PARTS["LeftLowerLeg"][0],
                               materials=("PickloHD_Gi",), subsurf=2)


def _build_foot(bpy, col):
    """Orange pointed shoe. Cross-sections swept from heel(+Y) to toe(-Y),
    each an ellipse; the sole (bottom faces) get material_index 1."""
    z_ank = L["ankle"]
    cx = _HX
    bm = bmesh.new()
    heel_y = 0.075
    toe_y = -0.195
    # centerline profile: (y, z_center, half_width, half_height, z is sole..top)
    # sole sits at z=0; shoe rises to ankle collar at the heel side.
    sections = [
        # y,      base_z, top_z,  half_w
        (heel_y,  0.006,  0.120,  0.047),   # heel back (collar rises)
        (0.045,   0.000,  0.150,  0.054),   # heel under ankle (collar wraps leg)
        (0.010,   0.000,  0.145,  0.056),   # ankle front
        (-0.035,  0.000,  0.070,  0.056),   # instep
        (-0.090,  0.002,  0.048,  0.052),   # mid foot
        (-0.140,  0.006,  0.040,  0.042),   # toe box
        (-0.175,  0.016,  0.038,  0.030),   # toe rises (upturn)
        (toe_y,   0.030,  0.042,  0.017),   # pointed upturned tip
    ]
    rings = []
    half = _NSEG // 2
    for (y, bz, tz, hw) in sections:
        czc = (bz + tz) * 0.5
        hh = (tz - bz) * 0.5
        row = []
        for i in range(_NSEG):
            a = -math.pi + 2.0 * math.pi * i / _NSEG
            c, s = math.cos(a), math.sin(a)  # param around cross-section
            # ellipse in (x, z): x across foot, z vertical
            x = cx + hw * math.sin(a)
            zz = czc - hh * math.cos(a)
            zz = max(zz, 0.0)  # flat-ish sole, no penetration below floor
            row.append(bm.verts.new((x, y, zz)))
        rings.append(row)
    _loft(bm, rings)
    _cap(bm, rings[0], +1)   # heel cap
    # toe tip cap
    tip = Vector((cx, toe_y - 0.012, 0.030))
    vc = bm.verts.new(tip)
    last = rings[-1]
    for i in range(_NSEG):
        bm.faces.new((last[i], last[(i + 1) % _NSEG], vc))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    # sole = downward-facing faces near the floor
    for f in bm.faces:
        if f.normal.z < -0.35 and f.calc_center_median().z < 0.030:
            f.material_index = 1
    return spec.object_from_bm(bpy, col, "LeftFoot", bm,
                               spec.PARTS["LeftFoot"][0],
                               materials=("PickloHD_Shoe", "PickloHD_Sole"),
                               subsurf=2)


def build(bpy, col):
    out = []
    lu = _build_upper(bpy, col)
    ll = _build_lower(bpy, col)
    lf = _build_foot(bpy, col)
    out += [lu, ll, lf]
    for src, name in ((lu, "RightUpperLeg"), (ll, "RightLowerLeg"),
                      (lf, "RightFoot")):
        out.append(spec.mirror_x(bpy, col, src, name, spec.PARTS[name][0]))
    return out
