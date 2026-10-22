import os
import tempfile
import time

import bpy

from . import capture, demo, deps, imaging, live
from .backends import MockBackend, ReactorBackend
from .prefs import get_api_key, get_prefs
from .props import get_settings, prompt_value
from .recorder import Recorder
from .session import UserError, get_session


def resolve_path(path):
    """bpy.path.abspath, but relative paths in an unsaved file go to the temp dir."""
    if path.startswith("//") and not bpy.data.filepath:
        return os.path.join(tempfile.gettempdir(), "eden_live_unsaved", path[2:])
    return bpy.path.abspath(path)


def eden_dir():
    return resolve_path("//eden_live/")


def backend_factory(context):
    """Return (factory, label) for the configured backend, or raise UserError."""
    prefs = get_prefs(context)
    if prefs is None or prefs.backend == "MOCK":
        latency = prefs.mock_latency if prefs else 1.0
        echo = prefs.mock_echo_tags if prefs else False
        return (lambda cb: MockBackend(cb, latency=latency, echo_tags=echo)), MockBackend.label
    ok, detail = deps.sdk_status()
    if not ok:
        raise UserError("Reactor SDK not available. Use a platform-specific build or Preferences > Add-ons > EdenLive > Install.")
    if not bpy.app.online_access:
        raise UserError("Online access is disabled: Preferences > System > Network > Allow Online Access.")
    key = get_api_key(context)
    if not key:
        raise UserError("Add your Reactor API key (Preferences > Add-ons > EdenLive, or the field in this panel).")
    session = get_session()
    session.add_secret(key)
    api_url = prefs.api_url.strip() or None
    max_seconds = prefs.max_session_minutes * 60

    def factory(cb):
        return ReactorBackend(cb, key, api_url=api_url, max_session_seconds=max_seconds)

    return factory, ReactorBackend.label


def reference_pixels_for(path, mock):
    """Validate the reference by decoding it; the mock also uses its palette."""
    try:
        pixels = capture.load_image_bgra(path, max_side=256)
    except Exception as exc:
        raise UserError(f"Could not read the reference image: {exc}")
    h, w = pixels.shape[:2]
    if w < 32 or h < 32:
        raise UserError("The reference image must be at least 32x32 pixels.")
    return pixels if mock else None


def capture_frame(context, settings, method=None):
    method = method or ("RENDER" if bpy.app.background else "VIEWPORT")
    if method == "RENDER":
        return capture.render_capture(context.scene)
    return live.get_capturer().capture(context, settings.source_mode, settings.hide_overlays)


class EDENLIVE_OT_connect(bpy.types.Operator):
    bl_idname = "eden_live.connect"
    bl_label = "Connect"
    bl_description = "Open an EdenLive session and start streaming the draft view"

    def execute(self, context):
        settings = get_settings(context)
        session = get_session()
        try:
            factory, label = backend_factory(context)
        except UserError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        session.fps = float(settings.fps)
        session.side_by_side = settings.show_draft
        live.ensure_preview_image()
        session.connect(factory, label)
        return {"FINISHED"}


class EDENLIVE_OT_disconnect(bpy.types.Operator):
    bl_idname = "eden_live.disconnect"
    bl_label = "Stop"
    bl_description = "End the EdenLive session (stops streaming and billing)"

    def execute(self, context):
        if live.state.recorder is not None:
            live.state.recorder.cancel()
        get_session().disconnect()
        return {"FINISHED"}


class EDENLIVE_OT_start(bpy.types.Operator):
    bl_idname = "eden_live.start"
    bl_label = "Start"
    bl_description = "Stage the reference and prompt, then create a new world (replaces the current one)"

    @classmethod
    def poll(cls, context):
        return get_session().ready

    def execute(self, context):
        settings = get_settings(context)
        session = get_session()
        path = resolve_path(settings.reference_path) if settings.reference_path else ""
        prompt = prompt_value(settings)
        try:
            if not path or not os.path.isfile(path):
                raise UserError("Pick a reference image or use 'Capture' first.")
            if not prompt:
                raise UserError("Write a prompt first.")
            pixels = reference_pixels_for(path, mock=session.backend_label == MockBackend.label)
        except UserError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        session.start_world(path, prompt, settings.seed, pixels)
        return {"FINISHED"}


class EDENLIVE_OT_pause(bpy.types.Operator):
    bl_idname = "eden_live.pause"
    bl_label = "Pause / Resume"
    bl_description = "Pause or resume the current world"

    @classmethod
    def poll(cls, context):
        session = get_session()
        return session.ready and session.state.get("started", False)

    def execute(self, context):
        session = get_session()
        session.set_paused(not session.state.get("paused", False))
        return {"FINISHED"}


class EDENLIVE_OT_send_prompt(bpy.types.Operator):
    bl_idname = "eden_live.send_prompt"
    bl_label = "Send Prompt"
    bl_description = "Send the prompt now (set_prompt, applies from the next chunk)"

    @classmethod
    def poll(cls, context):
        return get_session().ready

    def execute(self, context):
        prompt = prompt_value(get_settings(context))
        if not prompt:
            self.report({"ERROR"}, "The prompt is empty.")
            return {"CANCELLED"}
        session = get_session()
        session._sent_prompt = None
        session.set_prompt(prompt)
        return {"FINISHED"}


class EDENLIVE_OT_capture_reference(bpy.types.Operator):
    bl_idname = "eden_live.capture_reference"
    bl_label = "Capture"
    bl_description = (
        "Save the current draft view as the reference image. Restyle it in an image tool for best results; "
        "a raw draft as reference makes the output look like the draft"
    )

    def execute(self, context):
        settings = get_settings(context)
        try:
            frame = capture_frame(context, settings)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        path = os.path.join(eden_dir(), f"reference_{time.strftime('%Y%m%d_%H%M%S')}.png")
        capture.save_bgra_png(imaging.cover_resize(frame, imaging.EDEN_W, imaging.EDEN_H), path)
        settings.reference_path = path
        self.report({"INFO"}, f"Reference saved to {path}")
        return {"FINISHED"}


class EDENLIVE_OT_reference_from_output(bpy.types.Operator):
    bl_idname = "eden_live.reference_from_output"
    bl_label = "Use Output"
    bl_description = "Save the latest generated frame and use it as the reference (call Start to apply)"

    @classmethod
    def poll(cls, context):
        return get_session().main.get()[0] is not None

    def execute(self, context):
        frame, _ = get_session().main.get()
        path = os.path.join(eden_dir(), f"reference_output_{time.strftime('%Y%m%d_%H%M%S')}.png")
        capture.save_bgra_png(frame, path)
        get_settings(context).reference_path = path
        self.report({"INFO"}, f"Reference saved to {path}")
        return {"FINISHED"}


class EDENLIVE_OT_open_preview(bpy.types.Operator):
    bl_idname = "eden_live.open_preview"
    bl_label = "Open Preview Window"
    bl_description = "Open a window showing the live EdenLive output"

    def execute(self, context):
        image = live.ensure_preview_image()
        before = set(context.window_manager.windows[:])
        bpy.ops.wm.window_new()
        new = [w for w in context.window_manager.windows if w not in before]
        window = new[0] if new else context.window_manager.windows[-1]
        area = max(window.screen.areas, key=lambda a: a.width * a.height)
        area.type = "IMAGE_EDITOR"
        area.spaces.active.image = image

        def fit():
            for region in area.regions:
                if region.type == "WINDOW":
                    try:
                        with bpy.context.temp_override(window=window, area=area, region=region):
                            bpy.ops.image.view_all(fit_view=True)
                    except Exception:
                        pass
            return None

        bpy.app.timers.register(fit, first_interval=0.3)
        return {"FINISHED"}


def recorder_settings(context, settings):
    scene = context.scene
    session = get_session()
    prefs = get_prefs(context)
    path = resolve_path(settings.reference_path) if settings.reference_path else ""
    prompt = prompt_value(settings)
    if not path or not os.path.isfile(path):
        raise UserError("Pick a reference image or use 'Capture' first.")
    if not prompt:
        raise UserError("Write a prompt first.")
    if scene.camera is None:
        raise UserError("The scene needs an active camera to record.")
    mock = session.backend_label == MockBackend.label or (prefs is not None and prefs.backend == "MOCK")
    scene_fps = scene.render.fps / scene.render.fps_base
    stream_fps = max(10.0, min(60.0, scene_fps))
    out_dir = resolve_path(settings.record_dir or "//eden_live_renders/")
    os.makedirs(out_dir, exist_ok=True)
    return {
        "frames": range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)),
        "stream_fps": stream_fps,
        "output_fps": scene_fps,
        "out_dir": out_dir,
        "name": bpy.path.clean_name(settings.record_name or "take"),
        "tail_seconds": settings.record_tail,
        "reference_path": path,
        "prompt": prompt,
        "seed": settings.seed,
        "reference_pixels": reference_pixels_for(path, mock),
        "encoder": settings.record_encoder,
        "ffmpeg_path": bpy.path.abspath(prefs.ffmpeg_path) if prefs and prefs.ffmpeg_path else "",
        "keep_frames": settings.record_keep_frames,
        "warmup_timeout": 90.0,
    }


class EDENLIVE_OT_record(bpy.types.Operator):
    bl_idname = "eden_live.record"
    bl_label = "Record Timeline"
    bl_description = (
        "Render the frame range as drafts, play it through EdenLive at a steady rate, and save MP4s "
        "(Esc to cancel)"
    )

    _timer = None
    _frame_before = 1

    @classmethod
    def poll(cls, context):
        return live.state.recorder is None and get_session().status in ("ready", "connecting", "disconnected", "error")

    def invoke(self, context, event):
        settings = get_settings(context)
        session = get_session()
        try:
            rec_settings = recorder_settings(context, settings)
            if not session.ready and session.status != "connecting":
                factory, label = backend_factory(context)
                session.fps = rec_settings["stream_fps"]
                session.connect(factory, label)
            self.recorder = Recorder(session, rec_settings)
        except UserError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        live.state.recorder = self.recorder
        live.ensure_preview_image()
        self._frame_before = context.scene.frame_current
        self._method = settings.record_method
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        rec = self.recorder
        if event.type == "ESC" and event.value == "PRESS":
            rec.cancel()
        elif event.type == "TIMER":
            try:
                if rec.phase == "CAPTURE":
                    scene = context.scene
                    scene.frame_set(rec.frames[rec.index])
                    frame = capture_frame(context, get_settings(context), self._method)
                    if rec.add_frame(frame):
                        scene.frame_set(self._frame_before)
                        rec.begin_stream()
                else:
                    rec.poll()
            except Exception as exc:
                rec._fail(str(exc))
            live.redraw_panels()
        if rec.finished:
            return self._finish(context)
        return {"PASS_THROUGH"}

    def _finish(self, context):
        context.window_manager.event_timer_remove(self._timer)
        live.state.recorder = None
        live.state.last_recorder = self.recorder
        rec = self.recorder
        if rec.phase == "DONE":
            self.report({"INFO"}, f"{rec.message}: {rec.outputs['main']}")
            return {"FINISHED"}
        if rec.phase == "ERROR":
            self.report({"ERROR"}, rec.message)
        else:
            context.scene.frame_set(self._frame_before)
            self.report({"WARNING"}, rec.message)
        return {"CANCELLED"}


class EDENLIVE_OT_install_sdk(bpy.types.Operator):
    bl_idname = "eden_live.install_sdk"
    bl_label = "Install Reactor SDK"
    bl_description = "pip-install reactor-sdk into your Blender user directory (needs online access)"

    def execute(self, context):
        if not bpy.app.online_access:
            self.report({"ERROR"}, "Enable Preferences > System > Network > Allow Online Access first.")
            return {"CANCELLED"}
        deps.install_sdk_async()

        def refresh():
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            return 0.5 if deps.install_state()["running"] else None

        bpy.app.timers.register(refresh, first_interval=0.5)
        return {"FINISHED"}


class EDENLIVE_OT_build_demo(bpy.types.Operator):
    bl_idname = "eden_live.build_demo"
    bl_label = "Build Demo Scene"
    bl_description = "Create a new scene of colored primitives (a jungle temple courtyard) with an animated camera"

    def execute(self, context):
        demo.build_demo_scene()
        return {"FINISHED"}


classes = (
    EDENLIVE_OT_connect,
    EDENLIVE_OT_disconnect,
    EDENLIVE_OT_start,
    EDENLIVE_OT_pause,
    EDENLIVE_OT_send_prompt,
    EDENLIVE_OT_capture_reference,
    EDENLIVE_OT_reference_from_output,
    EDENLIVE_OT_open_preview,
    EDENLIVE_OT_record,
    EDENLIVE_OT_install_sdk,
    EDENLIVE_OT_build_demo,
)
