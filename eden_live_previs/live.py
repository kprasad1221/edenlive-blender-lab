"""Main-thread glue: a bpy timer that captures draft frames, debounces live prompt
edits, copies the newest generated frame into the preview Image, and redraws the UI.
"""

import logging
import time
from types import SimpleNamespace

import bpy

from . import capture
from .session import RateMeter, get_session

log = logging.getLogger(__name__)

PREVIEW_IMAGE = "EdenLive Preview"
TICK = 1.0 / 60.0
PROMPT_DEBOUNCE = 0.6
CAPTURE_DUTY_INVERSE = 2.5

state = SimpleNamespace(
    prompt_dirty_at=0.0,
    last_capture=0.0,
    capture_cost=0.0,
    capture_meter=RateMeter(),
    capture_error="",
    preview_serial=0,
    preview_size=None,
    capturer=None,
    recorder=None,
    last_recorder=None,
    last_redraw=0.0,
    reported_capture_error="",
)


def prompt_changed():
    state.prompt_dirty_at = time.perf_counter()


def get_capturer():
    if state.capturer is None:
        state.capturer = capture.DrawCapture()
        state.capturer.on_frame = _on_captured
    return state.capturer


def _on_captured(frame, seconds):
    state.capture_cost = seconds if state.capture_cost == 0 else 0.8 * state.capture_cost + 0.2 * seconds
    state.capture_meter.tick()
    session = get_session()
    if state.recorder is None and session.frame_provider is None:
        session.push_source_frame(frame)


def ensure_preview_image(width=1280, height=704):
    image = bpy.data.images.get(PREVIEW_IMAGE)
    if image is None:
        image = bpy.data.images.new(PREVIEW_IMAGE, width, height, alpha=False)
    elif tuple(image.size) != (width, height):
        image.scale(width, height)
    return image


def update_preview(session):
    item = session.take_preview(state.preview_serial)
    if item is None:
        return False
    width, height, pixels, serial = item
    image = ensure_preview_image(width, height)
    resized = state.preview_size != (width, height)
    state.preview_size = (width, height)
    image.pixels.foreach_set(pixels)
    image.update()
    state.preview_serial = serial
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR" and area.spaces.active.image == image:
                if resized:
                    fit_image_editor(window, area)
                area.tag_redraw()
    return True


def fit_image_editor(window, area):
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        return
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.image.view_all(fit_view=True)
    except Exception:
        log.debug("could not fit the preview", exc_info=True)


def redraw_panels():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()


def _tick():
    from .props import prompt_value

    session = get_session()
    context = bpy.context
    scene = context.scene
    if scene is None:
        return
    settings = scene.eden_live
    now = time.perf_counter()

    if session.ready and state.recorder is None and session.frame_provider is None:
        # Capturing holds the GIL; if it is slow (heavy scene, no GPU) capture less often
        # so the sender thread keeps its steady rate by repeating the newest frame.
        interval = max(1.0 / max(10, settings.fps), state.capture_cost * CAPTURE_DUTY_INVERSE)
        capturer = get_capturer()
        stalled = capturer.busy and now - capturer.requested_at > 1.0
        if now - state.last_capture >= interval and (not capturer.busy or stalled):
            state.last_capture = now
            try:
                capturer.request(settings.source_mode, settings.hide_overlays)
                state.capture_error = capturer.error or ("The 3D Viewport is not redrawing (minimized?)" if stalled else "")
            except capture.CaptureError as exc:
                state.capture_error = str(exc)
        if state.capture_error and state.capture_error != state.reported_capture_error:
            session._log("Capture: " + state.capture_error)
        state.reported_capture_error = state.capture_error

    if state.prompt_dirty_at and now - state.prompt_dirty_at > PROMPT_DEBOUNCE:
        state.prompt_dirty_at = 0.0
        if session.ready and session.state.get("started"):
            session.set_prompt(prompt_value(settings))

    update_preview(session)

    if session.consume_changes() or (session.status != "disconnected" and now - state.last_redraw > 0.5):
        state.last_redraw = now
        redraw_panels()


def tick():
    try:
        _tick()
    except Exception as exc:
        session = get_session()
        session._report_error(exc)
        if session.is_paid and not session._stop_requested.is_set():
            session.disconnect("Blender capture/preview error")
        log.exception("EdenLive tick failed")
    return TICK


def register():
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.5, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    if state.capturer is not None:
        try:
            state.capturer.remove()
        except Exception:
            pass
        state.capturer = None
