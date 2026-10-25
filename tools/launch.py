"""Load this checkout in a fresh Blender session, without installing over user add-ons."""
import os
import pathlib
import sys

import addon_utils
import bpy

ROOT = pathlib.Path(__file__).resolve().parents[1]
if bpy.app.version < (4, 2, 0):
    raise RuntimeError("Blender 4.2 or newer is required")
sys.path.insert(0, str(ROOT))
addon_utils.enable("eden_live_previs", default_set=False, persistent=False)
from eden_live_previs import demo, operators  # noqa: E402

runtime = bpy.context.window_manager.eden_live_runtime
runtime.backend_mode = "PAID" if os.environ.get("EDEN_LAB_MODE") == "PAID" else "MOCK"
if os.environ.get("EDEN_LAB_HAS_SCENE") != "1":
    scene = demo.build_demo_scene(bpy.context.scene)
    scene.frame_start, scene.frame_end = 1, 48
    scene.render.fps = 24

for window in bpy.context.window_manager.windows:
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            area.spaces.active.show_region_ui = True

print("EdenLive Lab ready. N > EdenLive. Capture, Check Inputs, then Connect.")
print("No session opened. The API key is never printed.")
