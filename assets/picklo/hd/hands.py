"""Hands for the HD Picklo (Piccolo) model.

Builds LeftHand (palm + four fingers + opposable thumb, relaxed downward hang
with a gentle curl, black nails on the fingertips) then mirrors it for
RightHand. Green skin (PickloHD_Skin) with PickloHD_Nail nail beds.

Local build frame is world: hand at wrist pivot (arm_x, 0, wrist); fingers
hang down (-Z) with a slight forward/inward curl; palm faces roughly inward
(toward the body). Nails sit on the back (outer, +X for the left hand) of the
last finger segment.
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
_AX = L["arm_x"]
_WR = L["wrist"]
_NSEG = 10  # verts around a finger


def _smooth01(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _tube(bm, path, radii, ellip, nail_last=False):
    """Loft a tube along path (list of Vector) with per-node radius. Returns
    the vert rings. If nail_last, tag +X-facing faces of the tip as nail."""
    rings = []
    prev_u = Vector((1.0, 0.0, 0.0))
    for k, c in enumerate(path):
        if k == 0:
            tan = (path[1] - path[0])
        elif k == len(path) - 1:
            tan = (path[-1] - path[-2])
        else:
            tan = (path[k + 1] - path[k - 1])
        tan.normalize()
        u = (prev_u - tan * prev_u.dot(tan))
        if u.length < 1e-6:
            u = Vector((0.0, 1.0, 0.0))
        u.normalize()
        v = tan.cross(u).normalized()
        prev_u = u
        r = radii[k]
        ex, ey = ellip
        row = []
        for i in range(_NSEG):
            a = 2.0 * math.pi * i / _NSEG
            co = c + u * (r * ex * math.cos(a)) + v * (r * ey * math.sin(a))
            row.append(bm.verts.new(co))
        rings.append(row)
    for a_row, b_row in zip(rings, rings[1:]):
        for i in range(_NSEG):
            j = (i + 1) % _NSEG
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    # round tip cap
    tip = path[-1] + (path[-1] - path[-2]).normalized() * radii[-1] * 0.6
    vc = bm.verts.new(tip)
    last = rings[-1]
    for i in range(_NSEG):
        f = bm.faces.new((last[i], last[(i + 1) % _NSEG], vc))
        if nail_last and f.calc_center_median().x > path[-1].x + radii[-1] * 0.2:
            f.material_index = 1
    # base cap
    first = rings[0]
    vc0 = bm.verts.new(path[0] - (path[1] - path[0]).normalized()
                       * radii[0] * 0.4)
    for i in range(_NSEG):
        bm.faces.new((first[(i + 1) % _NSEG], first[i], vc0))
    if nail_last:
        # tag the back (+X) faces of the last segment as nail bed
        z_tip = path[-1].z
        z_mid = path[-2].z
        for r0, r1 in zip(rings[-2:-1], rings[-1:]):
            pass
        for f in bm.faces:
            fc = f.calc_center_median()
            if fc.z < z_mid + 1e-4 and fc.x > path[-1].x + radii[-1] * 0.35:
                f.material_index = 1
    return rings


def _finger(bm, base, length, r0, r1, curl, splay):
    """Curved finger path from base hanging down with forward curl `curl`
    (radians total) and a lateral splay in Y. Knuckle bulges via radius."""
    nnode = 7
    path = []
    radii = []
    # build along an arc: start pointing down, rotate toward -Y (forward)
    ang = 0.0
    pos = base.copy()
    seglen = length / (nnode - 1)
    for k in range(nnode):
        t = k / (nnode - 1)
        path.append(pos.copy())
        # knuckle bulges at joints (t ~ 0.0, 0.4, 0.75)
        bulge = 1.0
        for jz in (0.0, 0.42, 0.78):
            bulge += 0.22 * math.exp(-((t - jz) / 0.10) ** 2)
        radii.append((r0 + (r1 - r0) * t) * bulge)
        # advance: direction rotates in the Y-Z plane (curl fwd) with splay X
        dirv = Vector((math.sin(splay) * 0.0,
                       -math.sin(ang),
                       -math.cos(ang)))
        dirv.x += splay  # slight lateral drift
        dirv.normalize()
        pos = pos + dirv * seglen
        ang += curl / (nnode - 1)
    return _tube(bm, path, radii, ellip=(0.86, 1.0), nail_last=True)


def _build_hand(bpy, col):
    bm = bmesh.new()
    wrist = Vector((_AX, 0.0, _WR))
    # palm block: rounded box spanning Y (width), thin in X, from wrist down
    palm_top = _WR - 0.006
    palm_bot = _WR - 0.085
    palm_rings = []
    nrow = 6
    for k in range(nrow):
        t = k / (nrow - 1)
        z = palm_top - (palm_top - palm_bot) * t
        w = 0.040 + 0.006 * math.sin(math.pi * t)  # slight knuckle spread
        d = 0.017 + 0.004 * (1.0 - t)
        cx = _AX - 0.004 * t   # palm cups slightly inward
        row = []
        for i in range(_NSEG + 6):
            a = 2.0 * math.pi * i / (_NSEG + 6)
            c, s = math.cos(a), math.sin(a)
            # cupped: palm side (-X) slightly concave
            dd = d * (1.0 - 0.18 * max(0.0, -math.cos(a)))
            row.append(bm.verts.new((cx + dd * (-c), w * s, z)))
        palm_rings.append(row)
    npv = _NSEG + 6
    for a_row, b_row in zip(palm_rings, palm_rings[1:]):
        for i in range(npv):
            j = (i + 1) % npv
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    # cap palm top (wrist join)
    ctop = Vector((_AX, 0.0, palm_top + 0.01))
    vt = bm.verts.new(ctop)
    for i in range(npv):
        bm.faces.new((palm_rings[0][(i + 1) % npv], palm_rings[0][i], vt))

    # four fingers hanging from palm bottom, spread in Y
    finger_defs = [
        # (y, length, r0, r1, curl, splay)  index..pinky
        (-0.030, 0.090, 0.0125, 0.0080, 0.55, -0.05),
        (-0.010, 0.100, 0.0130, 0.0082, 0.50, -0.02),
        (+0.010, 0.094, 0.0128, 0.0080, 0.52, +0.02),
        (+0.030, 0.076, 0.0115, 0.0074, 0.60, +0.06),
    ]
    for (y, length, r0, r1, curl, splay) in finger_defs:
        base = Vector((_AX - 0.006, y, palm_bot + 0.006))
        _finger(bm, base, length, r0, r1, curl, splay)

    # opposable thumb off the front-inner base of the palm
    thumb_base = Vector((_AX - 0.010, -0.036, palm_top - 0.028))
    tnode = 6
    path = []
    radii = []
    pos = thumb_base.copy()
    dirv = Vector((-0.35, -0.55, -0.75)).normalized()
    for k in range(tnode):
        t = k / (tnode - 1)
        path.append(pos.copy())
        bulge = 1.0 + 0.20 * math.exp(-((t - 0.45) / 0.14) ** 2)
        radii.append((0.0135 + (0.0085 - 0.0135) * t) * bulge)
        # thumb curls forward
        dirv = (dirv + Vector((0.0, -0.06, -0.02))).normalized()
        pos = pos + dirv * 0.018
    _tube(bm, path, radii, ellip=(0.9, 1.0), nail_last=True)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "LeftHand", bm,
                               spec.PARTS["LeftHand"][0],
                               materials=("PickloHD_Skin", "PickloHD_Nail"),
                               subsurf=2)


def build(bpy, col):
    lh = _build_hand(bpy, col)
    rh = spec.mirror_x(bpy, col, lh, "RightHand", spec.PARTS["RightHand"][0])
    return [lh, rh]
