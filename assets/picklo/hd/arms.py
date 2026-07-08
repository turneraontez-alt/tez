"""Arms for the HD Picklo (Piccolo) model.

Builds LeftUpperArm + LeftLowerArm as lofted tubes, then mirrors both for the
Right* twins. The arms hang straight down along x = +-arm_x.

- UpperArm: a rounded green-SKIN deltoid cap at the shoulder that seats into
  the torso armhole, transitioning into the PINK RIBBED muscle-wrap for the
  rest of the upper arm (pronounced ring ribs + a front bicep bulge).
- LowerArm: tighter ribbed wrap tapering toward the wrist, ending in two
  red-orange BAND rings and a sliver of green skin right at the wrist.
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

_NSEG = 24
L = spec.L
_AX = L["arm_x"]
_SHX = L["shoulder_x"]


def _smooth01(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _loft(bm, rings, cap_top=True, cap_bot=True):
    """rings: list of dict(z, cx, cy, r, ell, rib_amp, rib_ph, front). Build a
    tube; returns (vert_rows, ordered by ring). front bulge biases -Y."""
    rows = []
    for rg in rings:
        row = []
        for i in range(_NSEG):
            a = 2.0 * math.pi * i / _NSEG
            c, s = math.cos(a), math.sin(a)  # c=+1 front(-Y) via -c*ry below
            r = rg["r"] * (1.0 + rg["rib_amp"] * math.sin(rg["rib_ph"]))
            # front/back mass bias (bicep): extra depth where facing -Y
            front = rg.get("front", 0.0)
            rx = r * rg["ell"]
            ry = r * (1.0 + front * max(0.0, c))
            x = rg["cx"] + rx * s
            y = rg["cy"] - ry * c
            row.append(bm.verts.new((x, y, rg["z"])))
        rows.append(row)
    for a_row, b_row in zip(rows, rows[1:]):
        for i in range(_NSEG):
            j = (i + 1) % _NSEG
            bm.faces.new((a_row[i], a_row[j], b_row[j], b_row[i]))
    if cap_top:
        _cap(bm, rows[0], up=+1)
    if cap_bot:
        _cap(bm, rows[-1], up=-1)
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


def _mat_by_z(bm, bands):
    """Assign material_index per face by z-region. bands: list of (zlo,zhi,idx)
    checked in order; default 0."""
    for f in bm.faces:
        z = f.calc_center_median().z
        for zlo, zhi, idx in bands:
            if zlo <= z < zhi:
                f.material_index = idx
                break


def _build_upper(bpy, col):
    z_sh, z_el = L["shoulder"], L["elbow"]
    # deltoid tucks just under the gi shoulder seam (not perched on top)
    top = z_sh + 0.012
    rings = []
    n = 22
    for k in range(n):
        t = k / (n - 1)
        z = top - (top - (z_el - 0.01)) * t
        # lateral center drifts from shoulder_x (in) out to arm_x
        cx = _SHX + (_AX - _SHX) * _smooth01(min(1.0, t * 1.6))
        # radius: rounded deltoid shoulder easing smoothly into the ribbed arm
        if t < 0.22:  # deltoid shoulder cap (only a touch wider than the arm)
            tt = t / 0.22
            r = 0.047 + 0.010 * math.sin(math.pi * (0.5 + 0.5 * tt))
            rib_amp = 0.02 * tt
        else:
            tt = (t - 0.22) / 0.78
            r = 0.050 - 0.010 * tt
            rib_amp = 0.14
        rib_ph = (z - z_el) * 150.0
        front = 0.26 * (1.0 - abs(t - 0.55) / 0.55)  # bicep bulge mid-arm
        rings.append(dict(z=z, cx=cx, cy=0.0, r=r, ell=0.98,
                          rib_amp=rib_amp, rib_ph=rib_ph, front=max(0.0, front)))
    bm = bmesh.new()
    _loft(bm, rings)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    # material: 0=Rib (default), 1=Skin — green only over the rounded shoulder
    _mat_by_z(bm, [(z_sh - 0.05, top + 0.1, 1)])
    return spec.object_from_bm(bpy, col, "LeftUpperArm", bm,
                               spec.PARTS["LeftUpperArm"][0],
                               materials=("PickloHD_Rib", "PickloHD_Skin"),
                               subsurf=2)


def _build_lower(bpy, col):
    z_el, z_wr = L["elbow"], L["wrist"]
    top = z_el + 0.01
    bot = z_wr - 0.006
    rings = []
    n = 24
    for k in range(n):
        t = k / (n - 1)
        z = top - (top - bot) * t
        cx = _AX
        # taper toward wrist; two band rings just above wrist; skin at very end
        r = 0.045 - 0.014 * _smooth01(t)
        rib_amp = 0.11 if t < 0.80 else 0.02
        rib_ph = (z - z_wr) * 190.0
        if t > 0.86:  # wrist bands swell slightly
            r *= 1.10
            rib_amp = 0.0
        front = 0.16 * (1.0 - t)  # forearm flexor mass near elbow
        rings.append(dict(z=z, cx=cx, cy=0.0, r=r, ell=0.96,
                          rib_amp=rib_amp, rib_ph=rib_ph, front=max(0.0, front)))
    bm = bmesh.new()
    _loft(bm, rings)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    # 0=Rib default, 1=Band (two rings above wrist), 2=Skin (wrist end)
    band_hi = z_wr + 0.055
    band_lo = z_wr + 0.018
    _mat_by_z(bm, [(z_wr - 0.1, band_lo, 2),
                   (band_lo, band_hi, 1)])
    return spec.object_from_bm(bpy, col, "LeftLowerArm", bm,
                               spec.PARTS["LeftLowerArm"][0],
                               materials=("PickloHD_Rib", "PickloHD_Band",
                                          "PickloHD_Skin"),
                               subsurf=2)


def build(bpy, col):
    lu = _build_upper(bpy, col)
    ll = _build_lower(bpy, col)
    ru = spec.mirror_x(bpy, col, lu, "RightUpperArm",
                       spec.PARTS["RightUpperArm"][0])
    rl = spec.mirror_x(bpy, col, ll, "RightLowerArm",
                       spec.PARTS["RightLowerArm"][0])
    return [lu, ll, ru, rl]
