import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from . import deps


def get_prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_api_key(context=None):
    prefs = get_prefs(context)
    key = prefs.api_key.strip() if prefs else ""
    return key or os.environ.get("REACTOR_API_KEY", "").strip()


class EDENLIVE_AP_preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    api_key: StringProperty(
        name="Reactor API Key",
        description="Your rk_... key from the Reactor dashboard. Stored in Blender's user preferences, "
        "never in .blend files. Falls back to the REACTOR_API_KEY environment variable",
        subtype="PASSWORD",
    )
    backend: EnumProperty(
        name="Backend",
        items=[
            ("REACTOR", "Reactor EdenLive", "Stream to reactor/eden-live (needs an API key; billed per session)"),
            ("MOCK", "Offline mock", "Local stand-in that repaints the draft; for trying the workflow without a key"),
        ],
        default="REACTOR",
    )
    api_url: StringProperty(name="API URL", description="Leave empty for https://api.reactor.inc", default="")
    max_session_minutes: IntProperty(
        name="Max session length (min)",
        description="Reactor ends a session after this long, even if you forget to press Stop",
        default=30,
        min=1,
        max=1440,
    )
    mock_latency: FloatProperty(name="Mock latency (s)", default=1.0, min=0.0, max=5.0)
    mock_echo_tags: BoolProperty(
        name="Mock echoes frame tags", description="Exercise the tag-based alignment path", default=False
    )
    ffmpeg_path: StringProperty(
        name="ffmpeg", description="Optional ffmpeg executable for encoding; otherwise PATH, then Blender's own encoder",
        subtype="FILE_PATH",
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "backend")
        if self.backend == "REACTOR":
            col.prop(self, "api_key")
            if not self.api_key and os.environ.get("REACTOR_API_KEY"):
                col.label(text="Using REACTOR_API_KEY from the environment", icon="INFO")
            row = col.row()
            row.operator("wm.url_open", text="Get an API key", icon="URL").url = "https://reactor.inc/dashboard"
            col.prop(self, "max_session_minutes")
            col.prop(self, "api_url")
        else:
            col.prop(self, "mock_latency")
            col.prop(self, "mock_echo_tags")
        col.prop(self, "ffmpeg_path")

        box = layout.box()
        ok, detail = deps.sdk_status()
        box.label(text=f"Reactor SDK: {detail}", icon="CHECKMARK" if ok else "ERROR")
        if not ok:
            state = deps.install_state()
            row = box.row()
            row.enabled = not state["running"]
            row.operator("eden_live.install_sdk", icon="IMPORT")
            if state["log"]:
                for line in state["log"].splitlines():
                    box.label(text=line)
            if not bpy.app.online_access:
                box.label(text="Enable Preferences > System > Allow Online Access first", icon="INFO")
