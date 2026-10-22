"""Build a primitive-only demo scene: a jungle temple courtyard made of colored blocks
with a slow camera push-in. Colored cubes in, detailed world out.
"""

import math

import bpy

DEMO_PROMPT = (
    "A sunlit ancient temple courtyard deep in a lush jungle, cinematic and realistic. Weathered grey stone "
    "pillars and moss-covered stone steps lead to a carved golden statue. Tall tropical trees with dense green "
    "canopies, a calm turquoise pond, a sandy path. Golden hour light, soft haze. Keep the scene layout and "
    "camera movement. No text or HUD."
)

COLORS = {
    "ground": (0.22, 0.42, 0.16, 1.0),
    "path": (0.76, 0.66, 0.45, 1.0),
    "stone": (0.55, 0.55, 0.58, 1.0),
    "steps": (0.42, 0.40, 0.38, 1.0),
    "statue": (0.95, 0.72, 0.15, 1.0),
    "trunk": (0.35, 0.22, 0.10, 1.0),
    "leaves": (0.10, 0.60, 0.18, 1.0),
    "water": (0.10, 0.55, 0.75, 1.0),
    "sky": (0.55, 0.75, 0.95, 1.0),
}


def _material(name, color):
    mat = bpy.data.materials.get(f"EdenDemo_{name}") or bpy.data.materials.new(f"EdenDemo_{name}")
    mat.diffuse_color = color
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = color
        bsdf.inputs["Roughness"].default_value = 0.8
    return mat


def _add(scene, mesh_fn, name, color_key, location, scale=(1, 1, 1), rotation=(0, 0, 0), **kwargs):
    mesh = bpy.data.meshes.new(name)
    import bmesh

    bm = bmesh.new()
    mesh_fn(bm, **kwargs)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    obj.scale = scale
    obj.rotation_euler = rotation
    color = COLORS[color_key]
    obj.color = color
    mesh.materials.append(_material(color_key, color))
    scene.collection.objects.link(obj)
    return obj


def _cube(bm):
    import bmesh

    bmesh.ops.create_cube(bm, size=2.0)


def _cylinder(bm, segments=24, depth=2.0, r1=1.0, r2=1.0):
    import bmesh

    bmesh.ops.create_cone(bm, cap_ends=True, segments=segments, radius1=r1, radius2=r2, depth=depth)


def _sphere(bm, segments=24):
    import bmesh

    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=segments // 2, radius=1.0)


def clear_scene(scene):
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def build_demo_scene(scene=None, frames=120):
    if scene is None:
        scene = bpy.data.scenes.new("EdenLive Demo")
        window = bpy.context.window
        if window is not None:
            window.scene = scene
    clear_scene(scene)

    _add(scene, _cube, "Ground", "ground", (0, 0, -0.05), (30, 30, 0.05))
    _add(scene, _cube, "Path", "path", (0, -6, 0.01), (1.2, 8, 0.02))
    _add(scene, _cube, "Pond", "water", (-6, -4, 0.02), (3, 2.2, 0.02))
    for i in range(3):
        _add(scene, _cube, f"Step{i}", "steps", (0, 2.5 + i * 0.6, 0.15 + i * 0.3), (3.5 - i * 0.5, 0.6, 0.15 + i * 0.3))
    _add(scene, _cube, "Platform", "steps", (0, 5.5, 0.9), (4, 2.5, 0.9))
    _add(scene, _cylinder, "StatueBase", "stone", (0, 5.5, 2.1), (0.6, 0.6, 0.3))
    _add(scene, _cylinder, "Statue", "statue", (0, 5.5, 3.1), (0.35, 0.35, 0.7))
    _add(scene, _sphere, "StatueHead", "statue", (0, 5.5, 4.05), (0.3, 0.3, 0.3))
    for side in (-1, 1):
        for j, y in enumerate((-2.0, 0.5, 3.0, 5.5)):
            _add(scene, _cylinder, f"Pillar{side}_{j}", "stone", (side * 3.2, y, 1.6), (0.35, 0.35, 1.6))
            _add(scene, _cube, f"Cap{side}_{j}", "stone", (side * 3.2, y, 3.3), (0.5, 0.5, 0.12))
    trees = [(-8, 2), (-9, 7), (-5, 10), (7, 1), (9, 6), (6, 10), (-10, -2), (10, -3), (0, 12)]
    for k, (x, y) in enumerate(trees):
        height = 3.0 + (k % 3) * 0.8
        _add(scene, _cylinder, f"Trunk{k}", "trunk", (x, y, height / 2), (0.25, 0.25, height / 2))
        _add(scene, _sphere, f"Canopy{k}", "leaves", (x, y, height + 1.0), (1.8, 1.8, 1.4))

    cam_data = bpy.data.cameras.new("EdenCamera")
    cam_data.lens = 28
    camera = bpy.data.objects.new("EdenCamera", cam_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.rotation_euler = (math.radians(80), 0, 0)
    scene.frame_start = 1
    scene.frame_end = frames
    for frame, loc, rot_z in ((1, (1.5, -14, 2.2), -4.0), (frames, (-0.5, -4.5, 2.6), 4.0)):
        camera.location = loc
        camera.rotation_euler = (math.radians(82), 0, math.radians(rot_z))
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_euler", frame=frame)

    sun_data = bpy.data.lights.new("EdenSun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("EdenSun", sun_data)
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)

    world = scene.world or bpy.data.worlds.new("EdenDemoWorld")
    scene.world = world
    world.color = COLORS["sky"][:3]

    render = scene.render
    render.resolution_x = 1280
    render.resolution_y = 704
    render.resolution_percentage = 100
    render.fps = 24
    render.fps_base = 1.0
    render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "OBJECT"
    shading.background_type = "WORLD"

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                space.shading.type = "SOLID"
                space.shading.light = "STUDIO"
                space.shading.color_type = "OBJECT"
                space.shading.background_type = "WORLD"

    settings = scene.eden_live
    settings.prompt = DEMO_PROMPT
    scene.frame_set(1)
    return scene
