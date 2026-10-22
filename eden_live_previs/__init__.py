# SPDX-License-Identifier: MIT
"""EdenLive Pre-vis: stream a Blender scene through Reactor's EdenLive world model."""

bl_info = {
    "name": "EdenLive Pre-vis",
    "author": "EdenLive Pre-vis contributors",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > EdenLive",
    "description": "Turn a primitive Blender scene into live AI pre-vis with Reactor's EdenLive model",
    "doc_url": "https://github.com/willemhelmet/Eden-live-blender-reactor",
    "category": "3D View",
}

import atexit

import bpy

from . import deps, live, operators, prefs, props, ui
from .session import reset_session

_classes = (prefs.EDENLIVE_AP_preferences, props.EdenLiveSettings, *operators.classes, *ui.classes)


@bpy.app.handlers.persistent
def _on_exit(*_args):
    reset_session()


def register():
    deps.add_deps_path()
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.eden_live = bpy.props.PointerProperty(type=props.EdenLiveSettings)
    live.register()
    atexit.register(reset_session)
    if hasattr(bpy.app.handlers, "exit_pre") and _on_exit not in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.append(_on_exit)


def unregister():
    if hasattr(bpy.app.handlers, "exit_pre") and _on_exit in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.remove(_on_exit)
    live.unregister()
    atexit.unregister(reset_session)
    reset_session()
    del bpy.types.Scene.eden_live
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
