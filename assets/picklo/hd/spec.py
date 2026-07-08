"""Shared spec for the high-detail Picklo (Piccolo, Dragon Ball) model.

Every component module builds against THIS file: same landmarks, same part
names/pivots/hierarchy, same material registry, same finishing helpers.
Units are meters; the character stands on z=0, faces -Y, and the character's
LEFT is +X (matches the blocky asset in this directory).

REFERENCE (owner-supplied image, described): Piccolo mid-action. Green skin
with strong facial features: heavy scowling brow ridge, sharp long nose,
pointed chin, long pointed ears sweeping up-and-back, two thin segmented
antennae rising from the forehead and arcing forward, black/very dark nails,
stern eyes (white sclera, small dark iris). Sleeveless purple gi: loose top
with wide armholes, baggy pants gathered at the ankles. Wide blue obi sash
wrapped around the waist. Upper arms and forearms show PINK ribbed sections
(the classic ribbed "muscle wrap" look) with red-orange bands at the wrists.
Orange pointed shoes with lighter soles. Build is tall, lean, muscular.

Target look: "anime character rendered realistically" — smooth subdivided
organic surfaces, believable cloth and skin shading, no blocky geometry.
"""

# --------------------------------------------------------------------------
# Landmarks (z heights unless noted). Character crown ~1.92m, ~7.4 heads tall.
# --------------------------------------------------------------------------
L = {
    "sole": 0.0,
    "ankle": 0.09,
    "knee": 0.55,
    "hip": 1.02,          # LowerTorso pivot / hip joints
    "waist_top": 1.22,    # LowerTorso->UpperTorso split (sash upper edge)
    "shoulder": 1.58,     # shoulder joint height
    "neck": 1.63,         # UpperTorso->Head split
    "chin": 1.66,
    "crown": 1.92,
    "hip_x": 0.105,       # hip joint lateral offset
    "shoulder_x": 0.215,  # shoulder joint lateral offset
    "arm_x": 0.24,        # hanging arm axis lateral offset
    "elbow": 1.28,
    "wrist": 1.01,
    "fingertip": 0.82,
    "shoulder_w": 0.50,   # outer deltoid to outer deltoid
    "chest_d": 0.24,      # chest depth
    "waist_w": 0.32,
    "shoe_len": 0.28,     # heel to toe, toe points -Y
}

# name: (pivot, parent) — exactly these 15 objects, exactly these names.
# Extra detail objects (eyes, sash tails, ...) are allowed but MUST be
# parented under one of these 15 (e.g. eyes under Head).
PARTS = {
    "LowerTorso":    ((0.0, 0.0, L["hip"]), None),
    "UpperTorso":    ((0.0, 0.0, L["waist_top"]), "LowerTorso"),
    "Head":          ((0.0, 0.0, L["neck"]), "UpperTorso"),
    "LeftUpperArm":  ((+L["shoulder_x"], 0.0, L["shoulder"]), "UpperTorso"),
    "LeftLowerArm":  ((+L["arm_x"], 0.0, L["elbow"]), "LeftUpperArm"),
    "LeftHand":      ((+L["arm_x"], 0.0, L["wrist"]), "LeftLowerArm"),
    "RightUpperArm": ((-L["shoulder_x"], 0.0, L["shoulder"]), "UpperTorso"),
    "RightLowerArm": ((-L["arm_x"], 0.0, L["elbow"]), "RightUpperArm"),
    "RightHand":     ((-L["arm_x"], 0.0, L["wrist"]), "RightLowerArm"),
    "LeftUpperLeg":  ((+L["hip_x"], 0.0, L["hip"]), "LowerTorso"),
    "LeftLowerLeg":  ((+L["hip_x"], 0.0, L["knee"]), "LeftUpperLeg"),
    "LeftFoot":      ((+L["hip_x"], 0.0, L["ankle"]), "LeftLowerLeg"),
    "RightUpperLeg": ((-L["hip_x"], 0.0, L["hip"]), "LowerTorso"),
    "RightLowerLeg": ((-L["hip_x"], 0.0, L["knee"]), "RightUpperLeg"),
    "RightFoot":     ((-L["hip_x"], 0.0, L["ankle"]), "RightLowerLeg"),
}

# Material registry: name -> fallback sRGB color. materials.py registers the
# real node trees under these exact names; get_material() falls back to a
# flat placeholder so component modules can be tested standalone.
MATS = {
    "PickloHD_Skin":     (0.36, 0.60, 0.24),  # green skin (SSS)
    "PickloHD_SkinDark": (0.20, 0.35, 0.14),  # brow/lip/crease accents
    "PickloHD_Gi":       (0.36, 0.20, 0.55),  # purple cloth
    "PickloHD_Sash":     (0.13, 0.32, 0.65),  # blue obi silk
    "PickloHD_Rib":      (0.85, 0.55, 0.58),  # pink ribbed sections
    "PickloHD_Band":     (0.75, 0.25, 0.10),  # red-orange wrist bands
    "PickloHD_Shoe":     (0.80, 0.40, 0.08),  # orange shoe
    "PickloHD_Sole":     (0.85, 0.65, 0.30),  # lighter sole
    "PickloHD_Nail":     (0.04, 0.04, 0.045), # black glossy nails
    "PickloHD_EyeWhite": (0.92, 0.92, 0.90),
    "PickloHD_Pupil":    (0.03, 0.03, 0.03),
}

MODEL_COLLECTION = "PickloHD"


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def get_material(bpy, name):
    """Existing registered material, or a flat placeholder (standalone tests)."""
    mat = bpy.data.materials.get(name)
    if mat is None:
        rgb = MATS[name]
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        rgba = (*[srgb_to_linear(c) for c in rgb], 1.0)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = rgba
            bsdf.inputs["Roughness"].default_value = 0.6
        mat.diffuse_color = rgba
    return mat


def object_from_bm(bpy, col, name, bm, pivot, materials=("PickloHD_Skin",),
                   subsurf=2, smooth=True):
    """Finish a bmesh built in WORLD coordinates into a part object.

    Shifts verts so the object origin lands on `pivot` (the joint), links the
    object into `col`, assigns material slots in order (faces keep whatever
    material_index the builder set), smooth-shades, adds subsurf.
    """
    from mathutils import Vector

    for v in bm.verts:
        v.co -= Vector(pivot)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = pivot
    for mname in materials:
        obj.data.materials.append(get_material(bpy, mname))
    if smooth:
        for p in mesh.polygons:
            p.use_smooth = True
    if subsurf:
        mod = obj.modifiers.new("Subsurf", "SUBSURF")
        mod.levels = min(subsurf, 2)
        mod.render_levels = subsurf
    col.objects.link(obj)
    return obj


def mirror_x(bpy, col, src, name, pivot):
    """Mirrored (+X -> -X) copy of a finished part for the Right* twin."""
    mesh = src.data.copy()
    mesh.name = name
    obj = bpy.data.objects.new(name, mesh)
    obj.location = pivot
    obj.scale = (-1.0, 1.0, 1.0)
    for p in mesh.polygons:
        p.use_smooth = True
    for m in src.modifiers:
        mod = obj.modifiers.new(m.name, m.type)
        if m.type == "SUBSURF":
            mod.levels, mod.render_levels = m.levels, m.render_levels
    col.objects.link(obj)
    return obj


def reset_scene(bpy):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    col = bpy.data.collections.new(MODEL_COLLECTION)
    scene.collection.children.link(col)
    return scene, col


def test_render(bpy, out_prefix, samples=48, size=750):
    """Neutral test rig: frame everything in the model collection, render a
    front view (<prefix>_front.png) and a 3/4 view (<prefix>_34.png).
    Call AFTER building. Returns the two file paths."""
    import math
    from mathutils import Vector

    scene = bpy.context.scene
    col = bpy.data.collections.get(MODEL_COLLECTION)
    deps = bpy.context.evaluated_depsgraph_get()
    lo = Vector((1e9,) * 3)
    hi = Vector((-1e9,) * 3)
    for obj in col.objects:
        ev = obj.evaluated_get(deps)
        for corner in ev.bound_box:
            w = ev.matrix_world @ Vector(corner)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.05)

    world = bpy.data.worlds.new("W")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.75, 0.78, 0.82, 1.0)
        bg.inputs["Strength"].default_value = 0.6
    scene.world = world

    def light(name, kind, loc, energy, lsize=1.0):
        data = bpy.data.lights.new(name, type=kind)
        data.energy = energy
        if kind == "AREA":
            data.size = lsize
        ob = bpy.data.objects.new(name, data)
        ob.location = center + Vector(loc) * radius
        direction = center - ob.location
        ob.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        scene.collection.objects.link(ob)

    light("Key", "AREA", (2.0, -3.0, 2.5), 900 * radius * radius, 1.5 * radius)
    light("Fill", "AREA", (-2.5, -2.0, 0.8), 300 * radius * radius, 2.0 * radius)
    light("Rim", "AREA", (0.5, 3.0, 2.0), 600 * radius * radius, 1.5 * radius)

    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.device = "CPU"
    try:
        scene.cycles.use_denoising = True
    except Exception:
        pass
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.view_settings.view_transform = "Standard"

    cam_data = bpy.data.cameras.new("TestCam")
    cam = bpy.data.objects.new("TestCam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    paths = []
    dist = radius * 3.0
    for tag, ang in (("front", math.radians(90)), ("34", math.radians(55))):
        cam.location = center + Vector(
            (dist * math.cos(ang) * -1.0, -dist * math.sin(ang), radius * 0.35)
        )
        direction = center - cam.location
        cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
        path = f"{out_prefix}_{tag}.png"
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths.append(path)
    return paths
