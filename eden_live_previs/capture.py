"""Capture draft frames from Blender. Main thread only.

- ``DrawCapture`` / ``OffscreenCapture``: draws the scene from the active camera
  (or the current viewport view) into a GPU offscreen buffer using a 3D Viewport's
  shading settings, from inside that viewport's draw callback. Fast (a few ms),
  needs a visible 3D Viewport.
- ``render_capture``: renders with the scene's render engine (Workbench/EEVEE
  recommended). Slower, but works in background mode and uses final-render looks.
"""

import os
import tempfile
import time

import bpy
import numpy as np

from . import imaging


class CaptureError(Exception):
    pass


def find_view3d():
    """Return (window, area, region, space) of the largest visible 3D Viewport, or None."""
    best = None
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None or region.width < 8 or region.height < 8:
                continue
            if best is None or region.width * region.height > best[2].width * best[2].height:
                best = (window, area, region, area.spaces.active)
    return best


class OffscreenCapture:
    def __init__(self):
        self._offscreen = None
        self._size = (0, 0)

    def free(self):
        if self._offscreen is not None:
            self._offscreen.free()
        self._offscreen = None
        self._size = (0, 0)

    def _ensure(self, width, height):
        import gpu

        if self._size != (width, height) or self._offscreen is None:
            self.free()
            self._offscreen = gpu.types.GPUOffScreen(width, height)
            self._size = (width, height)
        return self._offscreen

    def capture(self, context, region, space, mode="CAMERA", hide_overlays=True,
                width=imaging.EDEN_W, height=imaging.EDEN_H):
        """Draw the view into the offscreen buffer; returns a BGRA uint8 frame (top row first).

        Must run inside a draw callback of ``region`` (see DrawCapture): drawing from a
        timer or operator uses whatever window's GPU context happens to be current and
        renders black once another window (such as the preview) has been drawn.
        """
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()
        if mode == "CAMERA":
            camera = scene.camera
            if camera is None:
                raise CaptureError("The scene has no active camera.")
            view_matrix = camera.matrix_world.inverted()
            projection = camera.calc_matrix_camera(
                depsgraph,
                x=width,
                y=height,
                scale_x=scene.render.pixel_aspect_x,
                scale_y=scene.render.pixel_aspect_y,
            )
        else:
            r3d = space.region_3d
            view_matrix = r3d.view_matrix.copy()
            projection = r3d.window_matrix.copy()
            width = max(2, int(round(height * region.width / region.height / 2.0)) * 2)

        offscreen = self._ensure(width, height)
        overlay = space.overlay
        had_overlays = overlay.show_overlays
        if hide_overlays:
            overlay.show_overlays = False
        try:
            offscreen.draw_view3d(
                scene,
                context.view_layer,
                space,
                region,
                view_matrix,
                projection,
                do_color_management=True,
            )
        finally:
            if hide_overlays:
                overlay.show_overlays = had_overlays
        # Read the texture rather than binding the framebuffer (framebuffers are per
        # GPU context). The Buffer is flattened because numpy misreads the
        # multi-dimensional ones, and converting element by element costs ~200 ms.
        buffer = offscreen.texture_color.read()
        buffer.dimensions = width * height * 4
        rgba = np.frombuffer(buffer, np.uint8).reshape(height, width, 4)
        return imaging.gpu_rgba_to_bgra(rgba)


class DrawCapture:
    """Captures frames from inside the 3D Viewport's draw callback.

    ``request()`` tags the largest visible 3D Viewport for redraw; its next draw
    renders the offscreen frame and hands it to ``on_frame(frame, seconds)``.
    """

    def __init__(self):
        self.offscreen = OffscreenCapture()
        self.handle = None
        self.pending = None
        self.requested_at = 0.0
        self.frame = None
        self.seq = 0
        self.error = ""
        self.on_frame = None

    def install(self):
        if self.handle is None:
            self.handle = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), "WINDOW", "POST_PIXEL")

    def remove(self):
        if self.handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.handle, "WINDOW")
            self.handle = None
        self.offscreen.free()

    @property
    def busy(self):
        return self.pending is not None

    def request(self, mode="CAMERA", hide_overlays=True):
        if bpy.app.background:
            raise CaptureError("Viewport capture needs Blender's UI; use the Render capture method in background mode.")
        found = find_view3d()
        if found is None:
            raise CaptureError("Open a 3D Viewport: it provides the shading used for the draft.")
        self.install()
        self.pending = (mode, hide_overlays, found[1].as_pointer())
        self.requested_at = time.perf_counter()
        found[1].tag_redraw()

    def cancel(self):
        self.pending = None

    def _draw(self):
        pending = self.pending
        context = bpy.context
        area = context.area
        if pending is None or area is None or area.as_pointer() != pending[2]:
            return
        self.pending = None
        start = time.perf_counter()
        try:
            frame = self.offscreen.capture(context, context.region, context.space_data, pending[0], pending[1])
        except Exception as exc:
            self.error = str(exc)
            return
        self.error = ""
        self.frame = frame
        self.seq += 1
        if self.on_frame is not None:
            self.on_frame(frame, time.perf_counter() - start)


def render_capture(scene, width=imaging.EDEN_W, height=imaging.EDEN_H, tmp_dir=None):
    """Render the current frame with the scene's engine; returns BGRA uint8."""
    if scene.camera is None:
        raise CaptureError("The scene has no active camera.")
    render = scene.render
    settings = render.image_settings
    saved = (
        render.resolution_x,
        render.resolution_y,
        render.resolution_percentage,
        render.filepath,
        render.use_file_extension,
        settings.file_format,
        settings.color_mode,
        settings.color_depth,
    )
    path = os.path.join(tmp_dir or tempfile.gettempdir(), "_eden_live_capture.png")
    try:
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.filepath = path
        render.use_file_extension = False
        settings.file_format = "PNG"
        settings.color_mode = "RGBA"
        settings.color_depth = "8"
        bpy.ops.render.render(write_still=True, scene=scene.name)
    finally:
        (
            render.resolution_x,
            render.resolution_y,
            render.resolution_percentage,
            render.filepath,
            render.use_file_extension,
            settings.file_format,
            settings.color_mode,
            settings.color_depth,
        ) = saved
    return load_image_bgra(path)


def load_image_bgra(path, max_side=None):
    """Load any image Blender can read as BGRA uint8 (top row first)."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        if width == 0 or height == 0:
            raise CaptureError(f"Could not read image: {path}")
        if max_side and max(width, height) > max_side:
            scale = max_side / max(width, height)
            image.scale(max(1, int(width * scale)), max(1, int(height * scale)))
            width, height = image.size
        pixels = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    return imaging.image_pixels_to_bgra(pixels, width, height)


def save_bgra_png(bgra, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    imaging.write_png(path, imaging.bgra_to_rgb(bgra), level=6)
    return path
