import os

import bpy
from bpy.props import StringProperty

from . import deps


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_api_key(context=None):
    prefs = get_prefs(context)
    key = prefs.api_key.strip() if prefs else ""
    return key or os.environ.get("REACTOR_API_KEY", "").strip()


def use_mock():
    """Developer switch: EDEN_LIVE_MOCK=1 replaces Reactor with the offline mock backend."""
    return os.environ.get("EDEN_LIVE_MOCK", "").strip().lower() in ("1", "true", "yes", "on")


class EDENLIVE_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    api_key: StringProperty(
        name="Reactor API Key",
        description="Your rk_... key from the Reactor dashboard. Stored in Blender's user preferences, "
        "never in .blend files. Falls back to the REACTOR_API_KEY environment variable",
        subtype="PASSWORD",
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "api_key")
        if not self.api_key and os.environ.get("REACTOR_API_KEY"):
            col.label(text="Using REACTOR_API_KEY from the environment", icon="INFO")
        col.operator("wm.url_open", text="Get an API key", icon="URL").url = "https://reactor.inc/dashboard"
        if use_mock():
            col.label(text="EDEN_LIVE_MOCK is set: using the offline mock backend", icon="INFO")

        ok, detail = deps.sdk_status()
        if ok:
            return
        box = layout.box()
        box.label(text=f"Reactor SDK: {detail}", icon="ERROR")
        state = deps.install_state()
        row = box.row()
        row.enabled = not state["running"]
        row.operator("eden_live.install_sdk", icon="IMPORT")
        for line in state["log"].splitlines():
            box.label(text=line)
        if not bpy.app.online_access:
            box.label(text="Enable Preferences > System > Allow Online Access first", icon="INFO")
