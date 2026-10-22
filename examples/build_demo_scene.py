"""Build the primitive demo scene (jungle temple courtyard made of colored blocks).

    blender --python examples/build_demo_scene.py                    # open it in the UI
    blender -b --python examples/build_demo_scene.py -- out.blend    # save it headless

Uses the installed add-on if it is enabled, otherwise this checkout.
The same scene is one click away in the add-on: Sidebar > EdenLive > Build Demo Scene.
"""

import os
import sys

import addon_utils
import bpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = "bl_ext.user_default.eden_live_previs"

if EXT in bpy.context.preferences.addons:
    module = EXT
else:
    sys.path.insert(0, ROOT)
    addon_utils.enable("eden_live_previs", default_set=True)
    module = "eden_live_previs"

demo = sys.modules[module + ".demo"]
scene = demo.build_demo_scene(bpy.context.scene)
print(f"Built '{scene.name}' with {len(scene.objects)} objects, frames {scene.frame_start}-{scene.frame_end}")

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if argv:
    path = os.path.abspath(argv[0])
    bpy.ops.wm.save_as_mainfile(filepath=path)
    print(f"Saved {path}")
