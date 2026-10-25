# SPDX-License-Identifier: MIT
"""EdenLive Pre-vis: stream a Blender scene through Reactor's EdenLive world model."""

bl_info = {
    "name": "EdenLive Lab",
    "author": "EdenLive Pre-vis contributors",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > EdenLive",
    "description": "Turn a primitive Blender scene into live AI pre-vis with Reactor's EdenLive model",
    "doc_url": "https://github.com/kprasad1221/edenlive-blender-lab/tree/codex/windows-testbench",
    "category": "3D View",
}

import atexit

import bpy

from . import deps, live, operators, prefs, props, ui
from .session import get_session, reset_session

_classes = (prefs.EDENLIVE_AP_preferences, props.EdenLiveSettings, props.EdenLiveRuntimeSettings,
            *operators.classes, *ui.classes)


@bpy.app.handlers.persistent
def _on_exit(*_args):
    reset_session()


@bpy.app.handlers.persistent
def _on_load(*_args):
    if live.state.recorder is not None:
        live.state.recorder.cancel()
        live.state.recorder = None
    session = get_session()
    if session.backend is not None or session.status == "connecting":
        try:
            closed = session.disconnect("Blender file change").result(timeout=7)
        except Exception:
            closed = False
        if closed:
            reset_session()
        # An unconfirmed close stays visible and retryable after the file changes.
    else:
        reset_session()
    runtime = getattr(bpy.context.window_manager, "eden_live_runtime", None)
    if runtime is not None:
        runtime.backend_mode = "MOCK"
    live.state.preview_serial = 0


def register():
    deps.add_deps_path()
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.eden_live = bpy.props.PointerProperty(type=props.EdenLiveSettings)
    bpy.types.WindowManager.eden_live_runtime = bpy.props.PointerProperty(type=props.EdenLiveRuntimeSettings)
    bpy.app.handlers.load_pre.append(_on_load)
    live.register()
    atexit.register(reset_session)
    if hasattr(bpy.app.handlers, "exit_pre") and _on_exit not in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.append(_on_exit)


def unregister():
    if _on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load)
    if live.state.recorder is not None:
        live.state.recorder.cancel()
        live.state.recorder = None
    if hasattr(bpy.app.handlers, "exit_pre") and _on_exit in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.remove(_on_exit)
    live.unregister()
    atexit.unregister(reset_session)
    reset_session()
    del bpy.types.Scene.eden_live
    del bpy.types.WindowManager.eden_live_runtime
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
