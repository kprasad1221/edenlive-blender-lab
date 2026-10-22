import bpy

from . import deps, live
from .prefs import get_api_key, get_prefs
from .session import get_session

STATUS_ICONS = {
    "disconnected": "UNLINKED",
    "connecting": "SORTTIME",
    "disconnecting": "SORTTIME",
    "ready": "LINKED",
    "error": "ERROR",
}


class EdenLivePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EdenLive"


class EDENLIVE_PT_main(EdenLivePanel, bpy.types.Panel):
    bl_label = "EdenLive Pre-vis"

    def draw(self, context):
        layout = self.layout
        session = get_session()
        prefs = get_prefs(context)
        mock = prefs is not None and prefs.backend == "MOCK"

        col = layout.column(align=True)
        status = session.status
        detail = f" ({session.remote_status})" if status == "connecting" and session.remote_status else ""
        label = session.backend_label or ("Mock (offline)" if mock else "Reactor")
        col.label(text=f"{label}: {status}{detail}", icon=STATUS_ICONS.get(status, "QUESTION"))
        if session.last_error:
            col.label(text=session.last_error, icon="ERROR")

        if prefs is not None:
            layout.prop(prefs, "backend", text="")
            if not mock:
                row = layout.row(align=True)
                row.prop(prefs, "api_key", text="API Key")
                if not prefs.api_key and get_api_key(context):
                    layout.label(text="Using REACTOR_API_KEY env var", icon="INFO")
                ok, _ = deps.sdk_status()
                if not ok:
                    layout.label(text="Reactor SDK missing: see add-on preferences", icon="ERROR")

        row = layout.row(align=True)
        row.scale_y = 1.3
        if status in ("ready", "connecting"):
            row.operator("eden_live.disconnect", icon="CANCEL")
        else:
            row.operator("eden_live.connect", icon="PLAY")
        layout.operator("eden_live.build_demo", icon="MESH_CUBE")


class EDENLIVE_PT_look(EdenLivePanel, bpy.types.Panel):
    bl_label = "Look"
    bl_parent_id = "EDENLIVE_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.eden_live
        layout.prop(settings, "reference_path", text="")
        row = layout.row(align=True)
        row.operator("eden_live.capture_reference", icon="RESTRICT_RENDER_OFF")
        row.operator("eden_live.reference_from_output", icon="IMAGE_DATA")

        layout.label(text="Prompt")
        if settings.prompt_text is None:
            layout.prop(settings, "prompt", text="")
        layout.prop(settings, "prompt_text", text="Text")
        row = layout.row(align=True)
        row.prop(settings, "live_prompt")
        row.operator("eden_live.send_prompt", text="Send", icon="EXPORT")
        layout.prop(settings, "seed")


class EDENLIVE_PT_live(EdenLivePanel, bpy.types.Panel):
    bl_label = "Live Preview"
    bl_parent_id = "EDENLIVE_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.eden_live
        session = get_session()
        st = session.state

        col = layout.column(align=True)
        col.prop(settings, "source_mode")
        col.prop(settings, "fps")
        col.prop(settings, "hide_overlays")

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("eden_live.start", icon="PLAY")
        paused = st.get("paused", False)
        row.operator(
            "eden_live.pause",
            text="Resume" if (paused and st.get("started")) else "Pause",
            icon="PLAY" if paused else "PAUSE",
        )

        layout.prop(settings, "show_draft")
        row = layout.row(align=True)
        row.operator("eden_live.open_preview", text="Preview: Split", icon="SPLIT_VERTICAL").target = "SPLIT"
        row.operator("eden_live.open_preview", text="Window", icon="WINDOW").target = "WINDOW"

        if session.status == "ready":
            box = layout.box().column(align=True)
            world = "none"
            if st.get("started"):
                world = f"#{st.get('world_id')} {'paused' if paused else 'running'}"
            box.label(text=f"World: {world}  chunks: {st.get('chunks', 0)}")
            secs = st.get("seconds")
            box.label(text=f"Sending {session.in_meter.rate():.0f} fps, receiving {session.out_meter.rate():.0f} fps")
            box.label(text=f"Draft capture {live.state.capture_meter.rate():.0f} fps "
                      f"({live.state.capture_cost * 1000:.0f} ms each)")
            if secs is not None:
                box.label(text=f"Last chunk took {secs:.2f} s")
        if live.state.capture_error:
            layout.label(text=live.state.capture_error, icon="ERROR")


class EDENLIVE_PT_record(EdenLivePanel, bpy.types.Panel):
    bl_label = "Record"
    bl_parent_id = "EDENLIVE_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.eden_live
        scene = context.scene
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(scene, "frame_start", text="Start")
        row.prop(scene, "frame_end", text="End")
        col.prop(settings, "record_method")
        col.prop(settings, "record_tail")
        col.prop(settings, "record_dir")
        col.prop(settings, "record_name")
        col.prop(settings, "record_encoder")
        col.prop(settings, "record_keep_frames")

        rec = live.state.recorder
        if rec is None:
            row = layout.row()
            row.scale_y = 1.3
            row.operator("eden_live.record", icon="REC")
            last = live.state.last_recorder
            if last is not None:
                layout.label(text=last.message, icon="CHECKMARK" if last.phase == "DONE" else "INFO")
                if last.kept_frames:
                    layout.operator("wm.path_open", text="Open Frames Folder", icon="FILE_FOLDER").filepath = (
                        last.kept_frames
                    )
                elif last.phase == "DONE":
                    layout.operator("wm.path_open", text="Open Output Folder", icon="FILE_FOLDER").filepath = (
                        last.out_dir
                    )
        else:
            layout.progress(factor=rec.progress, type="BAR", text=rec.message)
            layout.label(text="Esc cancels", icon="EVENT_ESC")


class EDENLIVE_PT_log(EdenLivePanel, bpy.types.Panel):
    bl_label = "Log"
    bl_parent_id = "EDENLIVE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in list(get_session().log)[-12:]:
            col.label(text=line)


classes = (EDENLIVE_PT_main, EDENLIVE_PT_look, EDENLIVE_PT_live, EDENLIVE_PT_record, EDENLIVE_PT_log)
