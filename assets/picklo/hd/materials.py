"""Realistic Cycles materials for the HD Picklo (Piccolo) model.

register(bpy) creates every material in spec.MATS under its exact name with a
Principled-BSDF node tree tuned for the intended surface (green SSS skin,
purple woven gi, blue silk sash, pink ribbed muscle-wrap, cloth bands, orange
leather shoe, matte sole, black lacquer nails, eyes). Idempotent: re-registers
by rebuilding the node tree of any same-name datablock (replacing the flat
placeholder spec.get_material() may have made).

Character scale is ~1.9 m, so all procedural bump features are deliberately
FINE (pores/weave in the millimetre range) — they must read as micro-surface,
not blobs.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import spec  # noqa: E402


def _lin(rgb):
    return (*[spec.srgb_to_linear(c) for c in rgb], 1.0)


def _fresh_tree(bpy, name):
    """Get-or-create a material and reset it to an empty node tree + BSDF/out."""
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat, nt, bsdf


def _set(bsdf, key, val):
    if key in bsdf.inputs:
        bsdf.inputs[key].default_value = val


def _noise_bump(nt, bsdf, scale, strength, detail=2.0, distortion=0.0,
                tex_coord="Object"):
    """Wire a Noise -> Bump into the BSDF Normal (fine micro-surface)."""
    tc = nt.nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, -300)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.location = (-500, -300)
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = detail
    if "Distortion" in noise.inputs:
        noise.inputs["Distortion"].default_value = distortion
    nt.links.new(tc.outputs[tex_coord], noise.inputs["Vector"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.location = (-150, -300)
    bump.inputs["Strength"].default_value = strength
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return tc, noise, bump


def _wave_bump(nt, bsdf, scale, strength, direction="X", distortion=1.0,
               detail=2.0):
    """Wire a Wave -> Bump for directional (ribbed / woven) micro-relief."""
    tc = nt.nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, -300)
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.location = (-500, -300)
    wave.wave_type = "BANDS"
    if hasattr(wave, "bands_direction"):
        wave.bands_direction = direction
    wave.inputs["Scale"].default_value = scale
    wave.inputs["Distortion"].default_value = distortion
    wave.inputs["Detail"].default_value = detail
    nt.links.new(tc.outputs["Object"], wave.inputs["Vector"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.location = (-150, -300)
    bump.inputs["Strength"].default_value = strength
    nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return tc, wave, bump


def _roughness_variation(nt, bsdf, base, scale, spread):
    """Break up a flat roughness with a large-scale noise so highlights vary."""
    tc = nt.nodes.new("ShaderNodeTexCoord")
    tc.location = (-800, 200)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.location = (-500, 200)
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = 2.0
    nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeMapRange")
    ramp.location = (-200, 200)
    ramp.inputs["To Min"].default_value = max(0.0, base - spread)
    ramp.inputs["To Max"].default_value = min(1.0, base + spread)
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Value"])
    nt.links.new(ramp.outputs["Result"], bsdf.inputs["Roughness"])


def _skin(bpy, name, rgb, dark=False):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Subsurface Weight", 0.16 if dark else 0.26)
    _set(bsdf, "Subsurface Scale", 0.03)
    if "Subsurface Radius" in bsdf.inputs:
        # greenish scatter: let green travel a touch further than red/blue
        bsdf.inputs["Subsurface Radius"].default_value = (0.010, 0.020, 0.008)
    _set(bsdf, "Specular IOR Level", 0.5)
    _roughness_variation(nt, bsdf, 0.38 if dark else 0.46, 12.0, 0.08)
    _noise_bump(nt, bsdf, scale=340.0, strength=0.09, detail=3.0)  # fine pores
    mat.diffuse_color = _lin(rgb)
    return mat


def _gi(bpy, name, rgb):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", 0.86)
    _set(bsdf, "Sheen Weight", 0.6)
    _set(bsdf, "Sheen Roughness", 0.4)
    _set(bsdf, "Specular IOR Level", 0.3)
    _wave_bump(nt, bsdf, scale=520.0, strength=0.05, direction="X",
               distortion=0.6, detail=2.0)
    mat.diffuse_color = _lin(rgb)
    return mat


def _sash(bpy, name, rgb):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", 0.34)
    _set(bsdf, "Anisotropic", 0.6)
    _set(bsdf, "Specular IOR Level", 0.6)
    _set(bsdf, "Sheen Weight", 0.3)
    _wave_bump(nt, bsdf, scale=380.0, strength=0.03, direction="Z",
               distortion=0.2, detail=1.0)
    mat.diffuse_color = _lin(rgb)
    return mat


def _rib(bpy, name, rgb):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Subsurface Weight", 0.12)
    _set(bsdf, "Subsurface Scale", 0.02)
    _set(bsdf, "Roughness", 0.5)
    _wave_bump(nt, bsdf, scale=90.0, strength=0.06, direction="Z",
               distortion=0.1, detail=1.0)  # longitudinal streaks
    mat.diffuse_color = _lin(rgb)
    return mat


def _cloth(bpy, name, rgb, rough):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", rough)
    _set(bsdf, "Sheen Weight", 0.4)
    _noise_bump(nt, bsdf, scale=300.0, strength=0.05, detail=2.0)
    mat.diffuse_color = _lin(rgb)
    return mat


def _shoe(bpy, name, rgb):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _roughness_variation(nt, bsdf, 0.45, 40.0, 0.12)
    _set(bsdf, "Specular IOR Level", 0.55)
    _noise_bump(nt, bsdf, scale=220.0, strength=0.10, detail=4.0,
                distortion=0.3)  # leather grain
    mat.diffuse_color = _lin(rgb)
    return mat


def _matte(bpy, name, rgb, rough=0.85):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", rough)
    _noise_bump(nt, bsdf, scale=160.0, strength=0.06)
    mat.diffuse_color = _lin(rgb)
    return mat


def _nail(bpy, name, rgb):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", 0.16)
    _set(bsdf, "Specular IOR Level", 0.8)
    _set(bsdf, "Coat Weight", 0.5)
    _set(bsdf, "Coat Roughness", 0.1)
    mat.diffuse_color = _lin(rgb)
    return mat


def _glossy(bpy, name, rgb, rough):
    mat, nt, bsdf = _fresh_tree(bpy, name)
    _set(bsdf, "Base Color", _lin(rgb))
    _set(bsdf, "Roughness", rough)
    _set(bsdf, "Specular IOR Level", 0.6)
    mat.diffuse_color = _lin(rgb)
    return mat


def register(bpy):
    M = spec.MATS
    _skin(bpy, "PickloHD_Skin", M["PickloHD_Skin"])
    _skin(bpy, "PickloHD_SkinDark", M["PickloHD_SkinDark"], dark=True)
    _gi(bpy, "PickloHD_Gi", M["PickloHD_Gi"])
    _sash(bpy, "PickloHD_Sash", M["PickloHD_Sash"])
    _rib(bpy, "PickloHD_Rib", M["PickloHD_Rib"])
    _cloth(bpy, "PickloHD_Band", M["PickloHD_Band"], rough=0.7)
    _shoe(bpy, "PickloHD_Shoe", M["PickloHD_Shoe"])
    _matte(bpy, "PickloHD_Sole", M["PickloHD_Sole"], rough=0.85)
    _nail(bpy, "PickloHD_Nail", M["PickloHD_Nail"])
    _glossy(bpy, "PickloHD_EyeWhite", M["PickloHD_EyeWhite"], rough=0.28)
    _glossy(bpy, "PickloHD_Pupil", M["PickloHD_Pupil"], rough=0.4)


if __name__ == "__main__":
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    register(bpy)
    print("registered:", sorted(m.name for m in bpy.data.materials
                                 if m.name.startswith("PickloHD_")))
