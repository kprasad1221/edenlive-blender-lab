import os
import tempfile
import time
import json
import platform

import bpy

from . import capture, demo, deps, imaging, live
from .backends import MockBackend, ReactorBackend, api_url
from .prefs import get_api_key, use_mock
from .props import get_settings, prompt_value
from .recorder import Recorder
from .session import UserError, get_session
from .safety import paid_limit


def resolve_path(path):
    """bpy.path.abspath, but relative paths in an unsaved file go to the temp dir."""
    if path.startswith("//") and not bpy.data.filepath:
        return os.path.join(tempfile.gettempdir(), "eden_live_unsaved", path[2:])
    return bpy.path.abspath(path)


def eden_dir():
    return resolve_path("//eden_live/")


def preflight(context, paid=False):
    """Local input validation only; never constructs or connects a Reactor client."""
    settings = get_settings(context)
    checks = []
    checks.append((bpy.app.version >= (4, 2, 0), "Blender 4.2 or newer"))
    checks.append((settings.source_mode != "CAMERA" or context.scene.camera is not None, "Active camera"))
    path = resolve_path(settings.reference_path) if settings.reference_path else ""
    valid_ref = False
    if path and os.path.isfile(path):
        try:
            reference_pixels_for(path, mock=True)
            valid_ref = True
        except UserError:
            pass
    checks.append((valid_ref, "Reference image (use Capture first)"))
    checks.append((bool(prompt_value(settings)), "Prompt"))
    if paid:
        ok, _ = deps.sdk_status()
        checks.extend([(ok, "Reactor SDK import"), (bool(bpy.app.online_access), "Allow Online Access"),
                       (bool(get_api_key(context)), "API key present")])
        try:
            official = api_url() == "https://api.reactor.inc"
        except ValueError:
            official = False
        checks.append((official, "Official Reactor API endpoint"))
    return checks


def backend_factory(context, paid_confirmed=False):
    """Return (factory, label) for the backend to use, or raise UserError."""
    if use_mock():
        return (lambda cb: MockBackend(cb)), MockBackend.label
    if not paid_confirmed:
        raise UserError("Paid connection requires explicit confirmation for this attempt.")
    failed = [name for ok, name in preflight(context, paid=True) if not ok]
    if failed:
        raise UserError("Check inputs first: " + "; ".join(failed))
    ok, detail = deps.sdk_status()
    if not ok:
        raise UserError("Reactor SDK not available. Use a platform-specific build or Preferences > Add-ons > EdenLive > Install.")
    if not bpy.app.online_access:
        raise UserError("Online access is disabled: Preferences > System > Network > Allow Online Access.")
    key = get_api_key(context)
    if not key:
        raise UserError("Add your Reactor API key (the field in this panel, or the add-on preferences).")
    try:
        url = api_url()
    except ValueError as exc:
        raise UserError(f"{exc} Check the REACTOR_API_URL environment variable.")
    get_session().add_secret(key)
    limit = paid_limit(context.window_manager.eden_live_runtime.paid_seconds)

    def factory(cb):
        return ReactorBackend(cb, key, api_url=url, max_session_seconds=limit)

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


VIEWPORT_TIMEOUT = 5.0


class ViewportFrameWaiter:
    """Request one frame from the 3D Viewport's draw callback and poll for it."""

    def __init__(self, settings, mode=None):
        self.capturer = live.get_capturer()
        self.seq = self.capturer.seq
        self.capturer.request(mode or settings.source_mode, settings.hide_overlays)
        self.started = time.perf_counter()

    def poll(self):
        """The frame once drawn, None while waiting; raises on timeout or error."""
        if self.capturer.seq != self.seq:
            return self.capturer.frame
        if self.capturer.error:
            raise capture.CaptureError(self.capturer.error)
        if time.perf_counter() - self.started > VIEWPORT_TIMEOUT:
            self.capturer.cancel()
            raise capture.CaptureError("The 3D Viewport did not redraw; keep it visible while capturing.")
        return None


class EDENLIVE_OT_connect(bpy.types.Operator):
    bl_idname = "eden_live.connect"
    bl_label = "Connect"
    bl_description = "Open an EdenLive session and start streaming the draft view"

    paid_confirmed: bpy.props.BoolProperty(name="I understand this uses Reactor credits", default=False,
                                           options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.paid_confirmed = False
        if use_mock(context):
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        seconds = context.window_manager.eden_live_runtime.paid_seconds
        self.layout.label(text=f"One paid attempt. Limit: {seconds}s including connection wait.")
        self.layout.label(text="Pause still uses session time. Stop requests session closure.")
        self.layout.prop(self, "paid_confirmed")

    def execute(self, context):
        settings = get_settings(context)
        session = get_session()
        try:
            factory, label = backend_factory(context, self.paid_confirmed)
            session.fps = float(settings.fps)
            session.side_by_side = settings.show_draft
            live.ensure_preview_image()
            session.connect(factory, label)
        except UserError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class EDENLIVE_OT_check_inputs(bpy.types.Operator):
    bl_idname = "eden_live.check_inputs"
    bl_label = "Check Inputs (free)"
    bl_description = "Validate local inputs and SDK availability without opening a paid session"

    def execute(self, context):
        try:
            checks = preflight(context, paid=not use_mock(context))
        except Exception as exc:
            get_session()._report_error(exc)
            self.report({"ERROR"}, get_session().redact(str(exc)))
            return {"CANCELLED"}
        for ok, name in checks:
            get_session()._log(f"Preflight {'PASS' if ok else 'FAIL'}: {name}")
        failed = sum(not ok for ok, _ in checks)
        message = f"{len(checks) - failed}/{len(checks)} checks passed; no session opened"
        context.window_manager.eden_live_runtime.preflight_summary = message
        self.report({"WARNING"} if failed else {"INFO"}, message)
        return {"FINISHED"}


class EDENLIVE_OT_export_debug(bpy.types.Operator):
    bl_idname = "eden_live.export_debug"
    bl_label = "Export Debug Report"
    bl_description = "Save redacted session stages, errors, timings and frame counts locally"
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        self.filepath = "edenlive-debug.json"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        session = get_session()
        report = session.debug_report()
        report["environment"] = {"blender": bpy.app.version_string, "python": platform.python_version(),
                                 "os": platform.system(), "architecture": platform.machine()}
        report["capture"] = {"fps": round(live.state.capture_meter.rate(), 2),
                             "last_error": session.redact(live.state.capture_error)}
        try:
            with open(bpy.path.abspath(self.filepath), "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except OSError as exc:
            self.report({"ERROR"}, session.redact(str(exc)))
            return {"CANCELLED"}
        self.report({"INFO"}, "Redacted report saved. Review it before sharing publicly.")
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


def save_reference(settings, frame):
    path = os.path.join(eden_dir(), f"reference_{time.strftime('%Y%m%d_%H%M%S')}.png")
    capture.save_bgra_png(imaging.cover_resize(frame, imaging.EDEN_W, imaging.EDEN_H), path)
    settings.reference_path = path
    return path


class EDENLIVE_OT_capture_reference(bpy.types.Operator):
    bl_idname = "eden_live.capture_reference"
    bl_label = "Capture"
    bl_description = (
        "Save the current draft view as the reference image. Restyle it in an image tool for best results; "
        "a raw draft as reference makes the output look like the draft"
    )

    def execute(self, context):
        settings = get_settings(context)
        if bpy.app.background:
            save_reference(settings, capture.render_capture(context.scene))
            return {"FINISHED"}
        try:
            waiter = ViewportFrameWaiter(settings)
        except capture.CaptureError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        scene_name = context.scene.name

        def wait():
            try:
                frame = waiter.poll()
            except capture.CaptureError as exc:
                live.state.capture_error = str(exc)
                return None
            if frame is None:
                return 0.05
            scene = bpy.data.scenes.get(scene_name)
            if scene is not None:
                save_reference(scene.eden_live, frame)
            return None

        bpy.app.timers.register(wait, first_interval=0.05)
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
    bl_label = "Show Preview"
    bl_description = "Show the live EdenLive output in an Image Editor"

    target: bpy.props.EnumProperty(
        name="Where",
        items=[
            ("SPLIT", "Split Viewport", "Split the 3D Viewport and show the output on the right"),
            ("WINDOW", "New Window", "Open a separate window (e.g. for a second monitor)"),
        ],
        default="SPLIT",
    )

    def execute(self, context):
        image = live.ensure_preview_image()
        wm = context.window_manager
        if self.target == "SPLIT":
            found = capture.find_view3d()
            if found is None:
                self.report({"ERROR"}, "No 3D Viewport to split.")
                return {"CANCELLED"}
            window, area, region, _space = found
            before = {a.as_pointer() for a in window.screen.areas}
            with context.temp_override(window=window, screen=window.screen, area=area, region=region):
                bpy.ops.screen.area_split(direction="VERTICAL", factor=0.5)
            new = [a for a in window.screen.areas if a.as_pointer() not in before]
            target = max([area, *new], key=lambda a: a.x)
        else:
            before = {w.as_pointer() for w in wm.windows}
            bpy.ops.wm.window_new()
            window = next((w for w in wm.windows if w.as_pointer() not in before), wm.windows[-1])
            target = max(window.screen.areas, key=lambda a: a.width * a.height)
        target.type = "IMAGE_EDITOR"
        target.spaces.active.image = image
        bpy.app.timers.register(lambda: live.fit_image_editor(window, target), first_interval=0.3)
        return {"FINISHED"}


def recorder_settings(context, settings):
    scene = context.scene
    session = get_session()
    path = resolve_path(settings.reference_path) if settings.reference_path else ""
    prompt = prompt_value(settings)
    if not path or not os.path.isfile(path):
        raise UserError("Pick a reference image or use 'Capture' first.")
    if not prompt:
        raise UserError("Write a prompt first.")
    if scene.camera is None:
        raise UserError("The scene needs an active camera to record.")
    mock = use_mock()
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
        "keep_frames": settings.record_keep_frames,
        "warmup_timeout": 30.0 if mock else 15.0,
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
    _waiter = None
    paid_confirmed: bpy.props.BoolProperty(name="I understand this uses Reactor credits", default=False,
                                           options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return live.state.recorder is None and get_session().status == "disconnected"

    def invoke(self, context, event):
        self.paid_confirmed = False
        if not use_mock(context):
            return context.window_manager.invoke_props_dialog(self, width=460)
        return self.execute(context)

    def draw(self, context):
        self.layout.label(text="Draft frames render locally before the paid session opens.")
        self.layout.label(text=f"Paid attempt limit: {context.window_manager.eden_live_runtime.paid_seconds}s.")
        self.layout.label(text="The session closes before MP4 encoding, including on failure.")
        self.layout.prop(self, "paid_confirmed")

    def execute(self, context):
        settings = get_settings(context)
        session = get_session()
        try:
            if session.status != "disconnected" or session.backend is not None:
                raise UserError("Stop live preview before recording; drafts must render without an open session.")
            rec_settings = recorder_settings(context, settings)
            factory, label = backend_factory(context, self.paid_confirmed)
            rec_settings.update(connect_factory=factory, connect_label=label)
            if label == ReactorBackend.label:
                duration = len(rec_settings["frames"]) / rec_settings["stream_fps"] + rec_settings["tail_seconds"]
                if duration + 15 > context.window_manager.eden_live_runtime.paid_seconds:
                    raise UserError("Shot is too long for this attempt limit; shorten the frame range (try 48 frames).")
            self.recorder = Recorder(session, rec_settings)
        except UserError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        live.state.recorder = self.recorder
        live.ensure_preview_image()
        self._frame_before = context.scene.frame_current
        self._method = "RENDER" if bpy.app.background else settings.record_method
        self._waiter = None
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
                    self._capture_step(context, rec)
                else:
                    rec.poll()
            except Exception as exc:
                rec._fail(str(exc))
            live.redraw_panels()
        if rec.finished:
            return self._finish(context)
        return {"PASS_THROUGH"}

    def _capture_step(self, context, rec):
        scene = context.scene
        if self._method == "RENDER":
            scene.frame_set(rec.frames[rec.index])
            frame = capture.render_capture(scene)
        elif self._waiter is None:
            scene.frame_set(rec.frames[rec.index])
            self._waiter = ViewportFrameWaiter(get_settings(context), mode="CAMERA")
            return
        else:
            frame = self._waiter.poll()
            if frame is None:
                return
            self._waiter = None
        if rec.add_frame(frame):
            scene.frame_set(self._frame_before)
            rec.begin_stream()

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
        context.scene.frame_set(self._frame_before)
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
    EDENLIVE_OT_check_inputs,
    EDENLIVE_OT_export_debug,
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
