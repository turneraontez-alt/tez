"""UpperTorso + LowerTorso for the HD Picklo (Piccolo) model.

UpperTorso: sleeveless loose purple gi — V-tapered silhouette, trapezius
slope up to a round rolled neck collar, clean elliptical armhole openings
finished with rolled-hem tubes, chest/lat/trap masses suggested under cloth,
and big soft vertical folds flaring toward the waist with a blouson tuck at
the sash line.

LowerTorso: wide blue obi sash modeled as three stacked, slightly tilted
wrap bands over a purple gi core (purple slivers show above and below the
sash edges), a two-lobe knot bump at the front-left hip, plus a "SashTails"
child object (two draping tail straps) parented under LowerTorso.
"""

import math
import os
import sys

import bmesh
from mathutils import Matrix, Vector

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import spec  # noqa: E402


# ---------------------------------------------------------------- helpers

_SEG = 32  # verts per torso ring


def _interp(z, table):
    """Smooth (cosine-eased) interpolation over rows (z, a, b, ...)."""
    if z <= table[0][0]:
        return tuple(table[0][1:])
    if z >= table[-1][0]:
        return tuple(table[-1][1:])
    for (za, *aa), (zb, *bb) in zip(table, table[1:]):
        if za <= z <= zb:
            f = (z - za) / (zb - za)
            f = f * f * (3.0 - 2.0 * f)
            return tuple(a + (b - a) * f for a, b in zip(aa, bb))
    return tuple(table[-1][1:])


def _sep(t, rx, ry, p):
    """Superellipse cross-section point. t=0 faces front (-Y)."""
    s, c = math.sin(t), math.cos(t)
    return (rx * math.copysign(abs(s) ** p, s),
            -ry * math.copysign(abs(c) ** p, c))


def _sep_n(x, y, rx, ry):
    v = Vector((x / (rx * rx), y / (ry * ry), 0.0))
    if v.length < 1e-9:
        return (0.0, -1.0)
    v.normalize()
    return (v.x, v.y)


def _dt(t, t0):
    return math.atan2(math.sin(t - t0), math.cos(t - t0))


def _g(d, w):
    return math.exp(-(d / w) ** 2)


def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _ramp(z, a, b):
    """0 at a -> 1 at b (either direction), smoothstepped."""
    if a == b:
        return 1.0
    x = _clamp01((z - a) / (b - a))
    return x * x * (3.0 - 2.0 * x)


def _fan_cap(bm, ring, sink=0.0):
    ctr = Vector((0.0, 0.0, 0.0))
    for v in ring:
        ctr += v.co
    ctr /= len(ring)
    ctr.z += sink
    vc = bm.verts.new(ctr)
    n = len(ring)
    faces = []
    for j in range(n):
        faces.append(bm.faces.new((ring[j], ring[(j + 1) % n], vc)))
    return faces


def _catmull(pts, n):
    """Catmull-Rom sample of control Vectors into n points."""
    pts = [pts[0]] + list(pts) + [pts[-1]]
    total = len(pts) - 3
    out = []
    for i in range(n):
        u = (i / (n - 1)) * total
        seg = min(int(u), total - 1)
        f = u - seg
        p0, p1, p2, p3 = pts[seg], pts[seg + 1], pts[seg + 2], pts[seg + 3]
        out.append(0.5 * ((2.0 * p1) + (-p0 + p2) * f
                          + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * (f * f)
                          + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * (f ** 3)))
    return out


# ------------------------------------------------------------ UpperTorso

# (z, rx, ry): loose-cloth half widths/depths of the gi.
_U_RXY = [
    (1.205, 0.172, 0.128),
    (1.235, 0.190, 0.142),
    (1.265, 0.194, 0.146),
    (1.320, 0.192, 0.145),
    (1.380, 0.190, 0.144),
    (1.430, 0.196, 0.147),
    (1.475, 0.206, 0.147),
    (1.515, 0.212, 0.142),
    (1.555, 0.210, 0.131),
    (1.578, 0.199, 0.121),
    (1.596, 0.176, 0.110),
    (1.612, 0.134, 0.095),
    (1.624, 0.095, 0.078),
    (1.632, 0.068, 0.063),
    (1.638, 0.056, 0.056),
]
# superellipse exponent per height (lower = boxier chest)
_U_P = [(1.205, 0.86), (1.430, 0.82), (1.500, 0.78),
        (1.580, 0.80), (1.610, 0.86), (1.638, 0.97)]

_U_RING_Z = [1.205, 1.230, 1.255, 1.285, 1.315, 1.345, 1.375, 1.405,
             1.435, 1.460, 1.482, 1.502, 1.522, 1.542, 1.562, 1.580,
             1.596, 1.610, 1.621, 1.630, 1.638]

# rolled neck collar (z, radius) continuing the loft past the top ring
_COLLAR = [(1.6445, 0.0595), (1.6490, 0.0545), (1.6435, 0.0475),
           (1.6290, 0.0450)]

# armhole opening: ellipse in (y, z) on each side wall
_AH_ZC = 1.545
_AH_AY = 0.068
_AH_AZ = 0.062

# big soft vertical fold ridges: (angle, amplitude)
_FOLDS = [(0.0, 0.012), (1.05, 0.019), (-1.05, 0.019),
          (2.2, 0.015), (-2.2, 0.015)]


def _u_rxy(z):
    r = _interp(z, _U_RXY)
    return r[0], r[1]


def _u_p(z):
    return _interp(z, _U_P)[0]


def _x_surf(y, z):
    """Torso surface |x| at (y, z) for the base superellipse profile."""
    rx, ry = _u_rxy(z)
    p = _u_p(z)
    u = min(abs(y) / max(ry, 1e-6), 0.9995)
    return rx * (1.0 - u ** (2.0 / p)) ** (p / 2.0)


def _upper_mod(t, z):
    """Per-vertex (radial, vertical) displacement: anatomy + cloth folds."""
    dr = 0.0
    dz = 0.0
    # vertical cloth folds, strongest toward the waist
    envf = _ramp(z, 1.47, 1.36) * (0.55 + 0.45 * _ramp(z, 1.205, 1.25))
    if envf > 0.001:
        for t0, a in _FOLDS:
            dr += a * envf * _g(_dt(t, t0), 0.30)
    # pecs under cloth + sternum crease
    envp = _g(z - 1.47, 0.055)
    dr += 0.016 * envp * (_g(_dt(t, 0.45), 0.42) + _g(_dt(t, -0.45), 0.42))
    dr -= 0.007 * envp * _g(_dt(t, 0.0), 0.13)
    # lat flare hint below the armholes
    envl = _g(z - 1.44, 0.035)
    dr += 0.007 * envl * (_g(_dt(t, 1.35), 0.25) + _g(_dt(t, -1.35), 0.25))
    # shoulder blades
    envb = _g(z - 1.49, 0.04)
    dr += 0.0065 * envb * (_g(_dt(t, math.pi - 0.65), 0.30)
                           + _g(_dt(t, -(math.pi - 0.65)), 0.30))
    # trapezius mounds + spine groove
    envt = _ramp(z, 1.555, 1.585) * _ramp(z, 1.625, 1.605)
    dr += 0.009 * envt * (_g(_dt(t, math.pi - 0.5), 0.35)
                          + _g(_dt(t, math.pi + 0.5), 0.35))
    dr -= 0.004 * envt * _g(_dt(t, math.pi), 0.15)
    # neckline dips slightly at the front
    dz += -0.007 * math.cos(t) * _ramp(z, 1.585, 1.625)
    # wavy blouson hem at the sash line
    dz += -0.004 * math.cos(2.0 * t + 0.7) * _ramp(z, 1.26, 1.205)
    return dr, dz


def _ah_e(co):
    return math.hypot(co.y / _AH_AY, (co.z - _AH_ZC) / _AH_AZ)


def _ah_side_ok(co, side):
    if not (1.45 < co.z < 1.632):
        return False
    rx, _ = _u_rxy(co.z)
    return co.x * side > 0.5 * rx


def _punch_armhole(bm, side):
    """Cut a clean elliptical armhole into one side wall + inner lip."""
    doomed = [f for f in bm.faces
              if len(f.verts) == 4
              and all(_ah_side_ok(v.co, side) and _ah_e(v.co) < 0.92
                      for v in f.verts)]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    # project ragged boundary verts onto the ellipse rim
    for v in bm.verts:
        if not _ah_side_ok(v.co, side):
            continue
        e = _ah_e(v.co)
        if 1e-6 < e < 1.0:
            y = v.co.y / e
            z = _AH_ZC + (v.co.z - _AH_ZC) / e
            v.co.y = y
            v.co.z = z
            v.co.x = side * _x_surf(y, z)
    vic = [v for v in bm.verts
           if _ah_side_ok(v.co, side) and _ah_e(v.co) < 1.06]
    bmesh.ops.remove_doubles(bm, verts=vic, dist=0.0045)
    # short inward lip so the opening has cloth thickness behind the hem
    bedges = [e for e in bm.edges
              if e.is_boundary
              and all(_ah_side_ok(v.co, side) and _ah_e(v.co) < 1.2
                      for v in e.verts)]
    if bedges:
        ret = bmesh.ops.extrude_edge_only(bm, edges=bedges)
        for ele in ret["geom"]:
            if isinstance(ele, bmesh.types.BMVert):
                ele.co.x -= side * 0.020
                ele.co.y *= 0.88
                ele.co.z = _AH_ZC + (ele.co.z - _AH_ZC) * 0.88


def _hem_tube(bm, side):
    """Rolled-hem torus riding the armhole rim on the torso surface."""
    NP, NT, RT = 28, 8, 0.0135
    centers, outwards = [], []
    for i in range(NP):
        ph = 2.0 * math.pi * i / NP
        y = _AH_AY * 1.05 * math.cos(ph)
        z = _AH_ZC + _AH_AZ * 1.05 * math.sin(ph)
        xs = _x_surf(y, z)
        rx, ry = _u_rxy(z)
        b = Vector((side * xs, y, z))
        n = Vector((b.x / (rx * rx), b.y / (ry * ry), 0.0))
        n.normalize()
        centers.append(b + n * 0.0045)
        outwards.append(n)
    rings = []
    for i in range(NP):
        tan = (centers[(i + 1) % NP] - centers[(i - 1) % NP]).normalized()
        bt = tan.cross(outwards[i]).normalized()
        nn = bt.cross(tan).normalized()
        ring = []
        for k in range(NT):
            a = 2.0 * math.pi * k / NT
            ring.append(bm.verts.new(centers[i]
                                     + nn * (RT * math.cos(a))
                                     + bt * (RT * math.sin(a))))
        rings.append(ring)
    for i in range(NP):
        r0, r1 = rings[i], rings[(i + 1) % NP]
        for k in range(NT):
            bm.faces.new((r0[k], r0[(k + 1) % NT],
                          r1[(k + 1) % NT], r1[k]))


def _build_upper(bpy, col):
    bm = bmesh.new()
    grid = []
    for z in _U_RING_Z:
        rx, ry = _u_rxy(z)
        p = _u_p(z)
        ring = []
        for j in range(_SEG):
            t = -math.pi + 2.0 * math.pi * j / _SEG
            bx, by = _sep(t, rx, ry, p)
            nx, ny = _sep_n(bx, by, rx, ry)
            dr, dz = _upper_mod(t, z)
            ring.append(bm.verts.new((bx + dr * nx, by + dr * ny, z + dz)))
        grid.append(ring)
    for z, r in _COLLAR:
        ring = []
        for j in range(_SEG):
            t = -math.pi + 2.0 * math.pi * j / _SEG
            dz = -0.007 * math.cos(t)
            ring.append(bm.verts.new((r * math.sin(t),
                                      -r * math.cos(t), z + dz)))
        grid.append(ring)
    for ra, rb in zip(grid, grid[1:]):
        for j in range(_SEG):
            bm.faces.new((ra[j], ra[(j + 1) % _SEG],
                          rb[(j + 1) % _SEG], rb[j]))
    _fan_cap(bm, grid[0], sink=+0.012)   # bottom (hidden in sash)
    _fan_cap(bm, grid[-1], sink=-0.004)  # inside the collar
    for side in (1.0, -1.0):
        _punch_armhole(bm, side)
    for side in (1.0, -1.0):
        _hem_tube(bm, side)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "UpperTorso", bm,
                               spec.PARTS["UpperTorso"][0],
                               materials=("PickloHD_Gi",), subsurf=2)


# ------------------------------------------------------------ LowerTorso

_L_RXY = [
    (1.015, 0.172, 0.131),
    (1.060, 0.168, 0.128),
    (1.110, 0.165, 0.126),
    (1.170, 0.1635, 0.1245),
    (1.225, 0.165, 0.126),
]
_LP = 0.85

# (z_center, half_height, outer_bulge, tilt, tilt_phase) per sash wrap
_WRAPS = [
    (1.088, 0.035, 0.016, 0.011, 0.6),
    (1.132, 0.035, 0.019, 0.013, 2.7),
    (1.172, 0.033, 0.022, 0.010, 4.9),
]

_KNOT_T = 0.62   # ring angle of the knot: front-left hip
_KNOT_Z = 1.100


def _l_rxy(z):
    r = _interp(z, _L_RXY)
    return r[0], r[1]


def _sash_band(bm, zc, h, dmax, tilt, phase):
    """One obi wrap: closed torus-profile band riding the gi core."""
    rx, ry = _l_rxy(zc)
    prof = [(-0.006, -0.90), (0.007, -1.00), (0.80 * dmax, -0.62),
            (dmax, -0.05), (0.92 * dmax, 0.55), (0.008, 1.00),
            (-0.006, 0.90), (-0.013, 0.0)]
    rows = []
    for j in range(_SEG):
        t = -math.pi + 2.0 * math.pi * j / _SEG
        zoff = tilt * math.sin(t + phase)
        ripple = 0.0018 * math.sin(4.0 * t + phase * 1.7)
        row = []
        for d, w in prof:
            dd = d + (ripple if d > 0.01 else 0.0)
            bx, by = _sep(t, rx + dd, ry + dd, _LP)
            row.append(bm.verts.new((bx, by, zc + zoff + w * h)))
        rows.append(row)
    np_ = len(prof)
    for j in range(_SEG):
        r0, r1 = rows[j], rows[(j + 1) % _SEG]
        for k in range(np_):
            bm.faces.new((r0[k], r0[(k + 1) % np_],
                          r1[(k + 1) % np_], r1[k]))


def _knot_frame():
    """(center, outward, tangent) of the sash knot on the wrap surface."""
    rx, ry = _l_rxy(_KNOT_Z)
    rxo, ryo = rx + 0.019, ry + 0.019
    bx, by = _sep(_KNOT_T, rxo, ryo, _LP)
    n = Vector((bx / (rxo * rxo), by / (ryo * ryo), 0.0)).normalized()
    c = Vector((bx, by, _KNOT_Z)) + n * 0.016
    tvec = Vector((-n.y, n.x, 0.0))
    return c, n, tvec


def _build_lower(bpy, col):
    bm = bmesh.new()
    zs = [1.015, 1.050, 1.085, 1.120, 1.155, 1.190, 1.225]
    grid = []
    for z in zs:
        rx, ry = _l_rxy(z)
        ring = []
        for j in range(_SEG):
            t = -math.pi + 2.0 * math.pi * j / _SEG
            bx, by = _sep(t, rx, ry, _LP)
            ring.append(bm.verts.new((bx, by, z)))
        grid.append(ring)
    for ra, rb in zip(grid, grid[1:]):
        for j in range(_SEG):
            bm.faces.new((ra[j], ra[(j + 1) % _SEG],
                          rb[(j + 1) % _SEG], rb[j]))
    _fan_cap(bm, grid[0], sink=+0.010)
    _fan_cap(bm, grid[-1], sink=-0.010)
    for f in bm.faces:
        f.material_index = 1  # purple gi core (slivers show past the sash)

    for zc, h, dmax, tilt, phase in _WRAPS:
        _sash_band(bm, zc, h, dmax, tilt, phase)

    # two-lobe knot bump at the front-left hip (new faces default to sash)
    c, n, tvec = _knot_frame()
    up = Vector((0.0, 0.0, 1.0))
    rot = Matrix((tvec, up, n)).transposed()
    m1 = Matrix.LocRotScale(c, rot, Vector((0.042, 0.030, 0.024)))
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8,
                              radius=1.0, matrix=m1)
    c2 = c + tvec * 0.027 + up * 0.017 - n * 0.005
    rot2 = rot @ Matrix.Rotation(0.4, 3, "X")
    m2 = Matrix.LocRotScale(c2, rot2, Vector((0.031, 0.023, 0.020)))
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8,
                              radius=1.0, matrix=m2)

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "LowerTorso", bm,
                               spec.PARTS["LowerTorso"][0],
                               materials=("PickloHD_Sash", "PickloHD_Gi"),
                               subsurf=2)


# -------------------------------------------------------------- SashTails

def _strap(bm, pts, w0, w1, th0, th1, twist):
    """Flat draping strap swept along pts, tapered, twisted, capped."""
    n = len(pts)
    NT = 10
    rings = []
    for i in range(n):
        tan = (pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]).normalized()
        f = i / (n - 1)
        hint = Vector((pts[i].x, pts[i].y, 0.0))
        if hint.length < 1e-5:
            hint = Vector((0.0, -1.0, 0.0))
        hint.normalize()
        wd = tan.cross(hint)
        if wd.length < 1e-4:
            wd = Vector((1.0, 0.0, 0.0))
        wd.normalize()
        dd = wd.cross(tan).normalized()
        ang = twist * f
        wd2 = wd * math.cos(ang) + dd * math.sin(ang)
        dd2 = dd * math.cos(ang) - wd * math.sin(ang)
        hw = (w0 + (w1 - w0) * f) * 0.5
        ht = (th0 + (th1 - th0) * f) * 0.5
        tip = 1.0 - 0.75 * max(0.0, (f - 0.85) / 0.15)
        ring = []
        for k in range(NT):
            a = 2.0 * math.pi * k / NT
            ca, sa = math.cos(a), math.sin(a)
            px = math.copysign(abs(ca) ** 0.72, ca)
            py = math.copysign(abs(sa) ** 0.72, sa)
            ring.append(bm.verts.new(pts[i] + wd2 * (hw * tip * px)
                                     + dd2 * (ht * tip * py)))
        rings.append(ring)
    for r0, r1 in zip(rings, rings[1:]):
        for k in range(NT):
            bm.faces.new((r0[k], r0[(k + 1) % NT],
                          r1[(k + 1) % NT], r1[k]))
    _fan_cap(bm, rings[0])
    _fan_cap(bm, rings[-1])


def _build_tails(bpy, col):
    c, n, tvec = _knot_frame()
    up = Vector((0.0, 0.0, 1.0))
    start = c + n * 0.002 - up * 0.020

    def wpts(offs):
        return [start + n * a + tvec * b + up * cz for a, b, cz in offs]

    ctrl_a = [(0.000, 0.004, 0.000), (0.012, 0.024, -0.045),
              (0.010, 0.034, -0.095), (-0.004, 0.024, -0.135),
              (-0.013, 0.008, -0.158)]
    ctrl_b = [(0.004, -0.008, 0.000), (0.018, -0.022, -0.040),
              (0.022, -0.032, -0.088), (0.012, -0.026, -0.128),
              (0.002, -0.012, -0.152)]
    bm = bmesh.new()
    _strap(bm, _catmull(wpts(ctrl_a), 12), 0.034, 0.020, 0.0075, 0.005, 0.6)
    _strap(bm, _catmull(wpts(ctrl_b), 12), 0.032, 0.019, 0.0075, 0.005, -0.5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return spec.object_from_bm(bpy, col, "SashTails", bm, tuple(c),
                               materials=("PickloHD_Sash",), subsurf=2)


# ------------------------------------------------------------------ build

def build(bpy, col):
    lower = _build_lower(bpy, col)
    upper = _build_upper(bpy, col)
    tails = _build_tails(bpy, col)
    bpy.context.view_layer.update()
    tails.parent = lower
    tails.matrix_parent_inverse = lower.matrix_world.inverted()
    return [lower, upper, tails]
